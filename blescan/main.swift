// Gibt jedes neue BLE-Advertisement mit Xiaomi-Servicedaten (0xFE95) als JSON-Zeile aus:
// {"id":"<CoreBluetooth-UUID>","rssi":-60,"name":"...","fe95":"<hex>"}
//
// Aufruf: blescan                      -> Zeilen auf stdout
//         blescan <programm> [args...] -> startet <programm> und schreibt die Zeilen in dessen stdin.
// Der zweite Modus ist nötig, weil macOS die Bluetooth-Freigabe an das gestartete Programm
// (diese Binary mit eingebetteter Info.plist) bindet, nicht an Python.
import CoreBluetooth
import Foundation

setvbuf(stdout, nil, _IOLBF, 0)

var out = FileHandle.standardOutput
let args = CommandLine.arguments
if args.count > 1 {
    let child = Process()
    child.executableURL = URL(fileURLWithPath: args[1])
    child.arguments = Array(args.dropFirst(2))
    let pipe = Pipe()
    child.standardInput = pipe
    child.terminationHandler = { p in
        FileHandle.standardError.write("child exited \(p.terminationStatus)\n".data(using: .utf8)!)
        exit(1)  // launchd startet neu
    }
    signal(SIGPIPE, SIG_IGN)
    try! child.run()
    out = pipe.fileHandleForWriting
}

final class Scanner: NSObject, CBCentralManagerDelegate {
    let fe95 = CBUUID(string: "FE95")
    var central: CBCentralManager!
    var last: [UUID: Data] = [:]
    var seen: [UUID: Date] = [:]

    override init() {
        super.init()
        central = CBCentralManager(delegate: self, queue: nil)
    }

    func centralManagerDidUpdateState(_ c: CBCentralManager) {
        switch c.state {
        case .poweredOn:
            c.scanForPeripherals(withServices: nil,
                                 options: [CBCentralManagerScanOptionAllowDuplicatesKey: true])
            FileHandle.standardError.write("scanning\n".data(using: .utf8)!)
        case .unauthorized:
            FileHandle.standardError.write("ERROR: Bluetooth nicht erlaubt\n".data(using: .utf8)!)
            exit(2)
        default:
            FileHandle.standardError.write("state \(c.state.rawValue)\n".data(using: .utf8)!)
        }
    }

    func centralManager(_ c: CBCentralManager, didDiscover p: CBPeripheral,
                        advertisementData ad: [String: Any], rssi: NSNumber) {
        guard let sd = ad[CBAdvertisementDataServiceDataKey] as? [CBUUID: Data],
              let d = sd[fe95] else { return }
        // Nur geänderte Pakete weitergeben – oder ein gleiches nach ≥5 s Funkstille
        // (so meldet sich die Waage beim Aufwachen, auch wenn ihr Ruhepaket gleich bleibt).
        let now = Date()
        let quiet = now.timeIntervalSince(seen[p.identifier] ?? .distantPast) >= 5
        seen[p.identifier] = now
        if last[p.identifier] == d && !quiet { return }
        last[p.identifier] = d
        let obj: [String: Any] = [
            "id": p.identifier.uuidString,
            "rssi": rssi.intValue,
            "name": (ad[CBAdvertisementDataLocalNameKey] as? String) ?? p.name ?? "",
            "fe95": d.map { String(format: "%02x", $0) }.joined(),
        ]
        if let json = try? JSONSerialization.data(withJSONObject: obj),
           let line = String(data: json, encoding: .utf8) {
            out.write((line + "\n").data(using: .utf8)!)
        }
    }
}

let scanner = Scanner()
RunLoop.main.run()
