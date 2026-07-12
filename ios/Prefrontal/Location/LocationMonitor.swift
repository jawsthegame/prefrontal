import Foundation
import CoreLocation

/// Opt-in geofencing (#469). Monitors the user's curated places (`/places`) with
/// `CLCircularRegion`s so crossing a boundary auto-logs without a Shortcut:
/// leaving the place named **home** posts `/webhooks/departure/left` (the native
/// replacement for the "when I leave Home" automation), and any enter/exit posts
/// the current position to `/webhooks/location`.
///
/// Region monitoring is battery-cheap (the OS wakes the app only on a boundary
/// crossing, even from terminated) — so `AppDelegate` touches `.shared` on every
/// launch to re-attach the delegate and receive events. Off unless the user opts
/// in (`AppConfig.locationEnabled`), which also gates the Always-location prompt.
final class LocationMonitor: NSObject, CLLocationManagerDelegate {
    static let shared = LocationMonitor()

    private let manager = CLLocationManager()
    private static let homeName = "home"
    private static let maxRegions = 18   // CoreLocation caps at 20 per app

    private override init() {
        super.init()
        manager.delegate = self
    }

    /// Called on launch: resume monitoring if the user previously opted in.
    func startIfEnabled() {
        guard SharedStore.locationEnabled else { return }
        refreshRegions()
    }

    /// Turn monitoring on — prompts for Always-location, then monitors places.
    func enable() {
        manager.requestAlwaysAuthorization()
        refreshRegions()
    }

    /// Turn monitoring off and drop every region.
    func disable() {
        for region in manager.monitoredRegions { manager.stopMonitoring(for: region) }
    }

    /// Replace the monitored set from the current `/places`.
    private func refreshRegions() {
        Task {
            guard let places = try? await withAPI({ try await $0.places() }) else { return }
            await MainActor.run { self.monitor(places) }
        }
    }

    private func monitor(_ places: [Place]) {
        for region in manager.monitoredRegions { manager.stopMonitoring(for: region) }
        for place in places.prefix(Self.maxRegions) {
            let region = CLCircularRegion(
                center: CLLocationCoordinate2D(latitude: place.lat, longitude: place.lon),
                radius: 120,
                identifier: place.name
            )
            region.notifyOnEntry = true
            region.notifyOnExit = true
            manager.startMonitoring(for: region)
        }
    }

    // MARK: - CLLocationManagerDelegate

    func locationManagerDidChangeAuthorization(_ manager: CLLocationManager) {
        switch manager.authorizationStatus {
        case .authorizedAlways, .authorizedWhenInUse:
            if SharedStore.locationEnabled { refreshRegions() }
        default:
            break
        }
    }

    func locationManager(_ manager: CLLocationManager, didExitRegion region: CLRegion) {
        let fix = manager.location
        let leftHome = region.identifier.lowercased() == Self.homeName
        Task {
            try? await withAPI { client in
                if let fix {
                    try await client.postLocation(
                        lat: fix.coordinate.latitude, lon: fix.coordinate.longitude,
                        accuracy: fix.horizontalAccuracy
                    )
                }
                if leftHome {
                    // Server defaults departed_at to now and uses the stored fix
                    // when lat/lon are omitted, so this works even without a fix.
                    try await client.postDepartureLeft(
                        lat: fix?.coordinate.latitude, lon: fix?.coordinate.longitude
                    )
                }
            }
        }
    }

    func locationManager(_ manager: CLLocationManager, didEnterRegion region: CLRegion) {
        guard let fix = manager.location else { return }
        Task {
            try? await withAPI {
                try await $0.postLocation(
                    lat: fix.coordinate.latitude, lon: fix.coordinate.longitude,
                    accuracy: fix.horizontalAccuracy
                )
            }
        }
    }
}
