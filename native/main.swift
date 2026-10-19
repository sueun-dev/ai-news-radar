import AppKit
import Foundation
import UserNotifications
import Darwin

final class RadarDelegate: NSObject, NSApplicationDelegate, UNUserNotificationCenterDelegate {
    let base = Bundle.main.object(forInfoDictionaryKey: "AIRadarBaseURL") as? String ?? "http://127.0.0.1:8741"
    var statusItem: NSStatusItem?
    var timer: Timer?
    var inFlight = Set<Int>()
    var ticks = 0
    var lockFD: Int32 = -1
    var pendingAcks: [Int: (String, String?)] = [:]
    var ackInFlight = Set<Int>()
    let center = UNUserNotificationCenter.current()

    func applicationDidFinishLaunching(_ notification: Notification) {
        if let directory = Bundle.main.object(forInfoDictionaryKey: "AIRadarDataDirectory") as? String {
            try? FileManager.default.createDirectory(atPath: directory, withIntermediateDirectories: true)
            lockFD = Darwin.open(directory + "/notifier.lock", O_CREAT | O_RDWR, mode_t(0o600))
            if lockFD < 0 || flock(lockFD, LOCK_EX | LOCK_NB) != 0 {
                NSApplication.shared.terminate(nil)
                return
            }
        }
        center.delegate = self
        statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
        statusItem?.button?.title = "AI Radar"
        let menu = NSMenu()
        menu.addItem(withTitle: "AI 소식과 감지 기록 열기", action: #selector(openDashboard), keyEquivalent: "")
        menu.addItem(withTitle: "시험 알림 보내기", action: #selector(testNotification), keyEquivalent: "")
        menu.addItem(NSMenuItem.separator())
        menu.addItem(withTitle: "알림 도우미 종료", action: #selector(quitBridge), keyEquivalent: "")
        for item in menu.items { item.target = self }
        statusItem?.menu = menu
        center.requestAuthorization(options: [.alert, .sound, .badge]) { granted, error in
            self.post("/api/bridge", ["authorization_requested": true, "granted": granted,
                                     "error": error?.localizedDescription ?? ""])
            self.sendSettings()
        }
        timer = Timer.scheduledTimer(withTimeInterval: 2, repeats: true) { _ in self.poll() }
        poll()
    }

    func post(_ path: String, _ data: [String: Any], done: ((Bool) -> Void)? = nil) {
        guard let url = URL(string: base + path),
              let body = try? JSONSerialization.data(withJSONObject: data) else { done?(false); return }
        var request = URLRequest(url: url, timeoutInterval: 5)
        request.httpMethod = "POST"
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.httpBody = body
        URLSession.shared.dataTask(with: request) { _, response, error in
            let code = (response as? HTTPURLResponse)?.statusCode ?? 0
            done?(error == nil && (200..<300).contains(code))
        }.resume()
    }

    func sendSettings() {
        center.getNotificationSettings { settings in
            self.post("/api/bridge", [
                "authorization": settings.authorizationStatus.rawValue,
                "alerts": settings.alertSetting.rawValue,
                "sounds": settings.soundSetting.rawValue,
                "note": "accepted means scheduled with macOS, not guaranteed visible or read"
            ])
        }
    }

    func poll() {
        for id in Array(pendingAcks.keys) { acknowledge(id) }
        ticks += 1
        if ticks % 10 == 0 { sendSettings() }
        guard let url = URL(string: base + "/api/pending") else { return }
        URLSession.shared.dataTask(with: URLRequest(url: url, timeoutInterval: 5)) { data, _, _ in
            guard let data = data,
                  let events = try? JSONSerialization.jsonObject(with: data) as? [[String: Any]] else { return }
            DispatchQueue.main.async {
                for event in events {
                    guard let id = event["id"] as? Int, !self.inFlight.contains(id) else { continue }
                    self.inFlight.insert(id)
                    self.deliver(event, id: id)
                }
            }
        }.resume()
    }

    func deliver(_ event: [String: Any], id: Int) {
        center.getNotificationSettings { settings in
            guard settings.authorizationStatus == .authorized || settings.authorizationStatus == .provisional else {
                self.finish(id, state: "denied", error: "macOS notification permission is not granted")
                return
            }
            let content = UNMutableNotificationContent()
            let isTest = (event["is_test"] as? Int ?? 0) == 1
            content.title = isTest ? "AI Radar · 자체 시험" : "AI 소식 · " + (event["source_id"] as? String ?? "")
            content.body = event["title"] as? String ?? ""
            content.sound = .default
            content.userInfo = ["event_id": id, "url": event["url"] as? String ?? self.base]
            let request = UNNotificationRequest(identifier: "radar-event-\(id)", content: content, trigger: nil)
            self.center.add(request) { error in
                self.finish(id, state: error == nil ? "accepted" : "error", error: error?.localizedDescription)
            }
        }
    }

    func finish(_ id: Int, state: String, error: String?) {
        DispatchQueue.main.async {
            self.pendingAcks[id] = (state, error)
            self.acknowledge(id)
        }
    }

    func acknowledge(_ id: Int) {
        guard let (state, error) = pendingAcks[id], !ackInFlight.contains(id) else { return }
        ackInFlight.insert(id)
        post("/api/notification-result", ["id": id, "state": state, "error": error ?? ""]) { accepted in
            DispatchQueue.main.async {
                self.ackInFlight.remove(id)
                if accepted {
                    self.pendingAcks.removeValue(forKey: id)
                    self.inFlight.remove(id)
                }
            }
        }
    }

    func userNotificationCenter(_ center: UNUserNotificationCenter,
                                willPresent notification: UNNotification,
                                withCompletionHandler completionHandler: @escaping (UNNotificationPresentationOptions) -> Void) {
        completionHandler([.banner, .list, .sound])
    }

    func userNotificationCenter(_ center: UNUserNotificationCenter, didReceive response: UNNotificationResponse,
                                withCompletionHandler completionHandler: @escaping () -> Void) {
        let info = response.notification.request.content.userInfo
        if let id = info["event_id"] as? Int { post("/api/opened", ["id": id]) }
        if let value = info["url"] as? String, let url = URL(string: value),
           url.scheme == "https" || url.host == "127.0.0.1" {
            DispatchQueue.main.async { NSWorkspace.shared.open(url) }
        }
        completionHandler()
    }

    @objc func openDashboard() { NSWorkspace.shared.open(URL(string: base)!) }
    @objc func testNotification() { post("/api/test", [:]) }
    @objc func quitBridge() { NSApplication.shared.terminate(nil) }
}

let app = NSApplication.shared
let delegate = RadarDelegate()
app.delegate = delegate
app.setActivationPolicy(.accessory)
app.run()
