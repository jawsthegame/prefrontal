import Foundation
import CoreLocation

/// Opt-in location monitoring. Two battery-cheap CoreLocation mechanisms feed
/// Prefrontal without any Shortcut, both waking the app even from terminated:
///
/// - **Geofences (#469, #563).** Curated places (`/places`) as `CLCircularRegion`s:
///   leaving the place named **home** posts `/webhooks/departure/left` (the
///   native replacement for the "when I leave Home" automation); arriving **home**
///   with an outing active posts `/webhooks/outing/return` to close it tap-free
///   (replacing the Tier-1 "I'm back" Shortcut); and any enter/exit posts the
///   current position to `/webhooks/location`.
/// - **Significant-location-change feed (#562).** Coarse (~500 m / cell-tower)
///   position updates keep `/webhooks/location` fresh *between* curated places —
///   what departure travel-time and trip stop-detection need — replacing the
///   Shortcuts "Update location" automation. Throttled to `minPostInterval` so a
///   burst of updates doesn't spam the endpoint.
///
/// `AppDelegate` touches `.shared` on every launch to re-attach the delegate and
/// resume both. Off unless the user opts in (`AppConfig.locationEnabled`), which
/// also gates the Always-location prompt.
final class LocationMonitor: NSObject, CLLocationManagerDelegate {
    static let shared = LocationMonitor()

    private let manager = CLLocationManager()
    private static let homeName = "home"
    private static let maxRegions = 18   // CoreLocation caps at 20 per app

    /// Minimum seconds between significant-change position posts, so a cluster of
    /// updates doesn't spam `/webhooks/location`. The cadence becomes
    /// web-configurable in #565; this is the default floor. Stored in the App
    /// Group so the throttle survives a terminated-state relaunch by the OS.
    private static let minPostInterval: TimeInterval = 300
    private static let lastPostKey = "lastLocationPostAt"

    private override init() {
        super.init()
        manager.delegate = self
    }

    /// Called on launch: resume monitoring if the user previously opted in.
    func startIfEnabled() {
        guard SharedStore.locationEnabled else { return }
        manager.startMonitoringSignificantLocationChanges()
        refreshRegions()
    }

    /// Turn monitoring on — prompts for Always-location, then monitors places
    /// and the significant-change position feed.
    func enable() {
        manager.requestAlwaysAuthorization()
        manager.startMonitoringSignificantLocationChanges()
        refreshRegions()
    }

    /// Turn monitoring off: stop the position feed and drop every region.
    func disable() {
        manager.stopMonitoringSignificantLocationChanges()
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
        let fix = manager.location
        let arrivedHome = region.identifier.lowercased() == Self.homeName
        Task {
            try? await withAPI { client in
                if let fix {
                    try await client.postLocation(
                        lat: fix.coordinate.latitude, lon: fix.coordinate.longitude,
                        accuracy: fix.horizontalAccuracy
                    )
                }
                // Arriving home is the native replacement for the Tier-1 "I'm
                // back" Shortcut (#563): close the active outing with no tap.
                // /webhooks/location only *stores* the fix — the server's passive
                // home-return close runs on a coach tick and is confirmation-
                // prompt + grace gated, so it neither fires off the location post
                // nor closes promptly. An explicit return on the (debounced,
                // 120 m) home-region entry restores the instant close. Gated on an
                // actually-active outing so a routine arrival home never posts a
                // spurious return (the endpoint 404s on none; this keeps it quiet).
                if arrivedHome {
                    let active = try await client.outings().active
                    if !active.isEmpty { try await client.returnOuting() }
                }
            }
        }
    }

    /// Significant-location-change updates — the coarse position feed (#562).
    func locationManager(_ manager: CLLocationManager, didUpdateLocations locations: [CLLocation]) {
        guard SharedStore.locationEnabled,
              let fix = locations.last,
              fix.horizontalAccuracy >= 0  // negative = invalid fix
        else { return }
        // Throttle across launches: SLC can relaunch us from terminated, so the
        // last-post time lives in the App Group rather than in memory.
        let now = Date().timeIntervalSince1970
        let last = SharedStore.defaults.double(forKey: Self.lastPostKey)
        guard now - last >= Self.minPostInterval else { return }
        SharedStore.defaults.set(now, forKey: Self.lastPostKey)
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
