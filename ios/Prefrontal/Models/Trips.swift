import Foundation

// Codable models for closed-loop trips — mirrors `GET /trips`
// (prefrontal/webhooks/routers/ingestion.py). A trip opens when you leave home
// and closes when you return; the system then asks you to name it. We decode a
// lean subset; unknown keys are ignored.

/// One round trip. `status` is active | completed. `label` is nil until you name
/// it (the "unlabeled" ask). `elapsedMinutes` is present only on the active trip;
/// completed trips carry `departedAt`/`returnedAt` (duration derived from them).
/// `suggestion` is present only on unlabeled trips whose stop reverse-matched a
/// curated place, to pre-fill the label form.
struct Trip: Codable, Identifiable {
    let id: Int
    let status: String
    let label: String?
    let category: String?
    let domain: String?
    let departedAt: String?
    let returnedAt: String?
    let maxDistanceM: Double?
    let reflection: String?
    let reflectionOutcome: String?
    let elapsedMinutes: Double?
    let suggestion: TripSuggestion?

    enum CodingKeys: String, CodingKey {
        case id, status, label, category, domain, reflection, suggestion
        case departedAt = "departed_at"
        case returnedAt = "returned_at"
        case maxDistanceM = "max_distance_m"
        case reflectionOutcome = "reflection_outcome"
        case elapsedMinutes = "elapsed_minutes"
    }

    var isActive: Bool { status == "active" }
    var isLabeled: Bool { !(label ?? "").isEmpty }

    /// Minutes out for a completed trip (departure → return), or nil if unknown.
    /// `GET /trips`' recent list carries the raw timestamps, not a computed
    /// duration, so derive it here — mirroring the server's `actual_minutes`.
    var durationMinutes: Int? {
        guard let start = PFDate.parse(departedAt), let end = PFDate.parse(returnedAt) else { return nil }
        return max(0, Int(end.timeIntervalSince(start) / 60))
    }

    /// "45 min" / "2h 10m" for a minutes count.
    static func minutesPhrase(_ minutes: Int) -> String {
        if minutes < 60 { return "\(minutes) min" }
        let h = minutes / 60, m = minutes % 60
        return m == 0 ? "\(h)h" : "\(h)h \(m)m"
    }

    /// "1.2 km" / "300 m" from the farthest-from-home distance, or nil.
    var distanceLabel: String? {
        guard let m = maxDistanceM, m > 0 else { return nil }
        if m >= 1000 { return String(format: "%.1f km", m / 1000) }
        return "\(Int(m.rounded())) m"
    }
}

/// A pre-fill guess for an unlabeled trip, from reverse-matching a stop to a
/// curated place (`{place, label, domain, distance_m}`).
struct TripSuggestion: Codable {
    let place: String?
    let label: String?
    let domain: String?
    let distanceM: Double?

    enum CodingKeys: String, CodingKey {
        case place, label, domain
        case distanceM = "distance_m"
    }
}

/// The `GET /trips` snapshot: the open trip, recent history, the completed trips
/// still awaiting a label, and the label-form vocabularies.
struct TripsSnapshot: Codable {
    let active: Trip?
    let recent: [Trip]
    let unlabeled: [Trip]
    let categories: [String]
    let domains: [String]
}
