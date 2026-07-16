import Foundation

// Brain-dump client models (roadmap M1 "capture at the speed of thought"). Split
// out of Models.swift to keep that file under the file-length ceiling. Lives in
// Models/ so the networking layer (compiled into the widget too) can reference
// these without importing the app-only, Foundation-Models-gated parser.

/// The framework-free result of an on-device parse (`BrainDumpParser`). `wireActions`
/// are `{op, …}` dictionaries in the exact shape `POST /braindump`'s `parse.actions`
/// expects and `POST /assistant/apply` echoes; `wireObservations` are the sensor
/// candidate objects (`{kind:"episode", …}`) `parse.observations` expects, which the
/// server validates against its allowlist and records **pending**.
struct ParsedBrainDump {
    /// A short, first-person acknowledgement of what was captured (never "done").
    let reply: String
    /// Server-ready wire actions. Each is a JSON-serializable `[String: Any]`.
    let wireActions: [[String: Any]]
    /// Server-ready sensor candidates (behavioral episodes). Empty when the ramble
    /// carried no behavioral aside — the common case. Each is a JSON-serializable
    /// `[String: Any]` in the shape `sensor.validate_observations` reads.
    let wireObservations: [[String: Any]]

    init(
        reply: String,
        wireActions: [[String: Any]],
        wireObservations: [[String: Any]] = []
    ) {
        self.reply = reply
        self.wireActions = wireActions
        self.wireObservations = wireObservations
    }
}

/// A minimal JSON value, so we can carry the assistant's heterogeneous action
/// dictionaries (`{op, summary, …arbitrary params}`) through the app and echo
/// them back to `POST /assistant/apply` byte-for-byte. Swift's `Codable` has no
/// built-in "any JSON" type; this is the small, well-known stand-in.
enum JSONValue: Codable, Hashable {
    case string(String)
    case int(Int)
    case double(Double)
    case bool(Bool)
    case object([String: JSONValue])
    case array([JSONValue])
    case null

    init(from decoder: Decoder) throws {
        let c = try decoder.singleValueContainer()
        if c.decodeNil() { self = .null; return }
        // Bool before Int: JSONDecoder would otherwise read `true` as 1.
        if let b = try? c.decode(Bool.self) { self = .bool(b); return }
        if let i = try? c.decode(Int.self) { self = .int(i); return }
        if let d = try? c.decode(Double.self) { self = .double(d); return }
        if let s = try? c.decode(String.self) { self = .string(s); return }
        if let o = try? c.decode([String: JSONValue].self) { self = .object(o); return }
        if let a = try? c.decode([JSONValue].self) { self = .array(a); return }
        throw DecodingError.dataCorruptedError(
            in: c, debugDescription: "Unsupported JSON value")
    }

    func encode(to encoder: Encoder) throws {
        var c = encoder.singleValueContainer()
        switch self {
        case let .string(s): try c.encode(s)
        case let .int(i): try c.encode(i)
        case let .double(d): try c.encode(d)
        case let .bool(b): try c.encode(b)
        case let .object(o): try c.encode(o)
        case let .array(a): try c.encode(a)
        case .null: try c.encodeNil()
        }
    }

    /// Back to a `JSONSerialization`-compatible value, for re-POSTing verbatim.
    var anyValue: Any {
        switch self {
        case let .string(s): return s
        case let .int(i): return i
        case let .double(d): return d
        case let .bool(b): return b
        case let .object(o): return o.mapValues { $0.anyValue }
        case let .array(a): return a.map { $0.anyValue }
        case .null: return NSNull()
        }
    }

    var stringValue: String? { if case let .string(s) = self { return s }; return nil }
}

/// One previewable editing action from `POST /braindump` — an `{op, summary, …}`
/// dictionary kept whole so it can be re-validated and echoed to
/// `POST /assistant/apply` unchanged. `op`/`summary` are surfaced for the review UI.
struct BrainDumpAction: Decodable, Hashable {
    let fields: [String: JSONValue]

    init(from decoder: Decoder) throws {
        fields = try decoder.singleValueContainer().decode([String: JSONValue].self)
    }

    var op: String { fields["op"]?.stringValue ?? "" }
    /// The server's one-line human description of the edit ("Add todo: …").
    var summary: String { fields["summary"]?.stringValue ?? op }
    /// The action as a `JSONSerialization`-ready dict, to POST back on Apply.
    var wire: [String: Any] { fields.mapValues { $0.anyValue } }
}

/// One pending behavioral candidate the server's sensor recorded from the dump —
/// mirrors `describe_proposal` (id/kind/summary/rationale/status).
struct BrainDumpProposal: Codable, Hashable, Identifiable {
    let id: Int
    let kind: String
    let summary: String
    let rationale: String
    let status: String
}

/// The `POST /braindump` response: a natural-language reply, a previewable action
/// list (nothing written yet), the reasons any items were dropped, the pending
/// behavioral proposals, and which provider handled each half ("on_device" when
/// the client sent a parse; else "anthropic"/"ollama").
struct BrainDumpResponse: Decodable {
    let reply: String
    let actions: [BrainDumpAction]
    let errors: [String]
    let proposals: [BrainDumpProposal]
    let provider: [String: String]?
}

/// Result of `POST /assistant/apply` — how many actions were written, plus a
/// per-action outcome row.
struct ApplyResult: Decodable {
    let applied: Int
    let results: [Row]
    let errors: [String]

    struct Row: Decodable, Hashable {
        let op: String
        let summary: String
        let ok: Bool
        let detail: String
    }
}
