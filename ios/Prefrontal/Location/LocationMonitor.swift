import Combine
import Foundation
import CoreLocation

/// Opt-in location monitoring. Three battery-cheap CoreLocation mechanisms feed
/// Prefrontal without any Shortcut, all waking the app even from terminated:
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
/// - **`CLVisit` monitoring (#564).** Arrivals/departures at *arbitrary* venues
///   (not just the ≤18 curated places) post their coordinate to
///   `/webhooks/location`, so a stop somewhere new is still visible natively.
///
/// All three `/webhooks/location` posts run through `postLocationDeduped`, which
/// collapses the double-post when two of them fire at one venue (e.g. the home
/// ring plus a `CLVisit`).
///
/// `AppDelegate` touches `.shared` on every launch to re-attach the delegate and
/// resume all three. Off unless the user opts in (`AppConfig.locationEnabled`),
/// which also gates the Always-location prompt.
final class LocationMonitor: NSObject, ObservableObject, CLLocationManagerDelegate {
    static let shared = LocationMonitor()

    /// The live CoreLocation authorization, mirrored for SwiftUI (the Settings
    /// location section observes it to show the true state and the right recovery
    /// affordance). Updated from `locationManagerDidChangeAuthorization`, which the
    /// singleton (created on the main thread) receives on the main thread.
    @Published private(set) var authorization: CLAuthorizationStatus = .notDetermined

    private let manager = CLLocationManager()
    private static let homeName = "home"
    private static let maxRegions = 18   // CoreLocation caps at 20 per app

    /// Minimum seconds between significant-change position posts, so a cluster of
    /// updates doesn't spam `/webhooks/location`. The cadence becomes
    /// web-configurable in #565; this is the default floor.
    private static let minPostInterval: TimeInterval = 300
    /// The significant-change feed's *own* throttle timestamp — advanced only by
    /// that feed, so an unrelated geofence/visit post never delays it. Stored in
    /// the App Group so the throttle survives a terminated-state relaunch.
    private static let lastSlcPostKey = "lastSlcPostAt"

    /// Shared de-dupe record for `/webhooks/location`: the coordinate + time of the
    /// last accepted post from *any* feed (geofence, visit, or significant-change).
    /// A fix within `dedupeRadius` of it, within `dedupeWindow`, is dropped —
    /// collapsing the double-post when a `CLVisit` and the curated-place geofence
    /// both fire at one venue (the home ring especially; a visit is detected
    /// minutes after arrival, once the geofence entry has already posted that
    /// coordinate). App-Group stored, so it holds across a terminated relaunch.
    private static let lastPostKey = "lastLocationPostAt"
    private static let lastPostLatKey = "lastLocationPostLat"
    private static let lastPostLonKey = "lastLocationPostLon"
    private static let dedupeWindow: TimeInterval = 120
    private static let dedupeRadius: CLLocationDistance = 150

    private override init() {
        super.init()
        manager.delegate = self
        authorization = manager.authorizationStatus
    }

    /// Called on launch: resume monitoring if the user previously opted in.
    func startIfEnabled() {
        guard SharedStore.locationEnabled else { return }
        manager.startMonitoringSignificantLocationChanges()
        manager.startMonitoringVisits()
        refreshRegions()
    }

    /// Turn monitoring on — prompts for Always-location, then monitors places,
    /// the significant-change position feed, and CLVisit arrivals/departures.
    func enable() {
        manager.requestAlwaysAuthorization()
        manager.startMonitoringSignificantLocationChanges()
        manager.startMonitoringVisits()
        refreshRegions()
    }

    /// Re-request Always after a While-Using grant — the one upgrade prompt iOS
    /// allows (subsequent asks are Settings-only). Drives the Settings upgrade row.
    func requestAlways() {
        manager.requestAlwaysAuthorization()
    }

    /// Turn monitoring off: stop the position feed and visits, drop every region.
    func disable() {
        manager.stopMonitoringSignificantLocationChanges()
        manager.stopMonitoringVisits()
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

    /// Post a fix to `/webhooks/location`, unless a near-identical one was posted
    /// moments ago (within `dedupeRadius` and `dedupeWindow`). Shared by the
    /// geofence, `CLVisit`, and significant-change feeds so two mechanisms firing
    /// at one venue don't double-post. The accepted fix's coordinate + time live
    /// in the App Group, so the guard holds across a terminated-state relaunch.
    private func postLocationDeduped(
        _ client: APIClient, lat: Double, lon: Double, accuracy: Double?
    ) async throws {
        let d = SharedStore.defaults
        let prevAt = d.double(forKey: Self.lastPostKey)
        let prevLat = d.double(forKey: Self.lastPostLatKey)
        let prevLon = d.double(forKey: Self.lastPostLonKey)
        if prevAt > 0, Date().timeIntervalSince1970 - prevAt < Self.dedupeWindow {
            let prev = CLLocation(latitude: prevLat, longitude: prevLon)
            if CLLocation(latitude: lat, longitude: lon).distance(from: prev) < Self.dedupeRadius {
                return  // same place, moments ago — the other feed already posted it
            }
        }
        // Claim the slot *before* awaiting the POST. This method runs on the main
        // actor, so read-check-stamp is atomic against other feeds' tasks (which
        // only run at an await point) — a geofence + significant-change update
        // firing together can't both pass the guard and double-post. On failure we
        // roll the record back so a transient error doesn't suppress a retry.
        d.set(Date().timeIntervalSince1970, forKey: Self.lastPostKey)
        d.set(lat, forKey: Self.lastPostLatKey)
        d.set(lon, forKey: Self.lastPostLonKey)
        do {
            try await client.postLocation(lat: lat, lon: lon, accuracy: accuracy)
        } catch {
            d.set(prevAt, forKey: Self.lastPostKey)
            d.set(prevLat, forKey: Self.lastPostLatKey)
            d.set(prevLon, forKey: Self.lastPostLonKey)
            throw error
        }
    }

    // MARK: - CLLocationManagerDelegate

    func locationManagerDidChangeAuthorization(_ manager: CLLocationManager) {
        authorization = manager.authorizationStatus
        switch manager.authorizationStatus {
        case .authorizedAlways, .authorizedWhenInUse:
            if SharedStore.locationEnabled { refreshRegions() }
        case .denied, .restricted:
            // Permission is gone — stop the now-silent monitoring. The Settings
            // section reconciles the stored opt-in (flips the toggle off) so the
            // in-app state matches reality.
            disable()
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
                    try await self.postLocationDeduped(
                        client, lat: fix.coordinate.latitude, lon: fix.coordinate.longitude,
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
        // Nothing to do for a non-home entry with no fix — skip building a client
        // and hopping to the main actor for a call we'd never make. Home arrivals
        // still proceed without a fix (the outing return needs no coordinates).
        guard fix != nil || arrivedHome else { return }
        Task {
            try? await withAPI { client in
                if let fix {
                    try await self.postLocationDeduped(
                        client, lat: fix.coordinate.latitude, lon: fix.coordinate.longitude,
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
        // Throttle across launches on the feed's own key (independent of
        // geofence/visit posts): SLC can relaunch us from terminated, so the
        // last-post time lives in the App Group rather than in memory.
        let now = Date().timeIntervalSince1970
        let last = SharedStore.defaults.double(forKey: Self.lastSlcPostKey)
        guard now - last >= Self.minPostInterval else { return }
        Task {
            try? await withAPI {
                try await self.postLocationDeduped(
                    $0, lat: fix.coordinate.latitude, lon: fix.coordinate.longitude,
                    accuracy: fix.horizontalAccuracy
                )
                // Advance the throttle only after a successful attempt (a posted
                // or intentionally de-duped call — not a thrown network error), so
                // a transient failure doesn't leave the feed stale for the whole
                // interval. A self-burst can't double-post: the de-dupe above
                // claims its slot synchronously.
                SharedStore.defaults.set(now, forKey: Self.lastSlcPostKey)
            }
        }
    }

    /// `CLVisit` arrivals/departures at *any* venue (#564) — not just the ≤18
    /// curated `/places` the geofences cover. `startMonitoringVisits` is
    /// battery-cheap and wakes the app from terminated. Both edges (arrival, when
    /// `departureDate == .distantFuture`, and departure) feed the visit coordinate
    /// to `/webhooks/location`, de-duped so a visit coinciding with a curated-place
    /// geofence crossing doesn't double-post. Closing an outing stays home-only
    /// (the home ring, #563): an arbitrary-venue arrival only refreshes position
    /// and lets the server decide whether that coordinate is home.
    func locationManager(_ manager: CLLocationManager, didVisit visit: CLVisit) {
        guard SharedStore.locationEnabled, visit.horizontalAccuracy >= 0 else { return }
        let coord = visit.coordinate
        let accuracy = visit.horizontalAccuracy
        Task {
            try? await withAPI {
                try await self.postLocationDeduped(
                    $0, lat: coord.latitude, lon: coord.longitude, accuracy: accuracy
                )
            }
        }
    }
}
