import Foundation

/// Arbitrary JSON, used for `Event.data` (a free-form dict on the wire).
enum JSONValue: Codable, Hashable, Sendable {
    case string(String)
    case number(Double)
    case bool(Bool)
    case object([String: JSONValue])
    case array([JSONValue])
    case null

    init(from decoder: Decoder) throws {
        let c = try decoder.singleValueContainer()
        if c.decodeNil() {
            self = .null
        } else if let b = try? c.decode(Bool.self) {
            self = .bool(b)
        } else if let n = try? c.decode(Double.self) {
            self = .number(n)
        } else if let s = try? c.decode(String.self) {
            self = .string(s)
        } else if let a = try? c.decode([JSONValue].self) {
            self = .array(a)
        } else if let o = try? c.decode([String: JSONValue].self) {
            self = .object(o)
        } else {
            throw DecodingError.dataCorruptedError(in: c, debugDescription: "Unsupported JSON value")
        }
    }

    func encode(to encoder: Encoder) throws {
        var c = encoder.singleValueContainer()
        switch self {
        case .string(let s): try c.encode(s)
        case .number(let n): try c.encode(n)
        case .bool(let b): try c.encode(b)
        case .object(let o): try c.encode(o)
        case .array(let a): try c.encode(a)
        case .null: try c.encodeNil()
        }
    }

    // MARK: Accessors

    var string: String? {
        if case .string(let s) = self { return s }
        return nil
    }

    var double: Double? {
        switch self {
        case .number(let n): return n
        case .string(let s): return Double(s)
        default: return nil
        }
    }

    var int: Int? { double.map { Int($0) } }

    var bool: Bool? {
        switch self {
        case .bool(let b): return b
        case .number(let n): return n != 0
        case .string(let s): return ["true", "1", "yes"].contains(s.lowercased())
        default: return nil
        }
    }

    var object: [String: JSONValue]? {
        if case .object(let o) = self { return o }
        return nil
    }

    var array: [JSONValue]? {
        if case .array(let a) = self { return a }
        return nil
    }

    subscript(key: String) -> JSONValue? {
        object?[key]
    }

    /// Short human representation, used for action args.
    var compact: String {
        switch self {
        case .string(let s): return s
        case .number(let n):
            if n.rounded() == n, abs(n) < 1e12 { return String(Int(n)) }
            return String(format: "%.1f", n)
        case .bool(let b): return b ? "true" : "false"
        case .null: return "null"
        case .array(let a): return "[" + a.map(\.compact).joined(separator: ",") + "]"
        case .object(let o):
            return "{" + o.keys.sorted().map { "\($0):\(o[$0]!.compact)" }.joined(separator: ",") + "}"
        }
    }

    /// Re-decode this JSON into a typed model (e.g. `data["agent"]` -> `Agent`).
    func decode<T: Decodable>(_ type: T.Type) -> T? {
        guard let data = try? JSONEncoder().encode(self) else { return nil }
        return try? JSONDecoder().decode(T.self, from: data)
    }

    /// Encode any model into JSONValue (used by demo mode to build events).
    init?<T: Encodable>(encoding value: T) {
        guard let data = try? JSONEncoder().encode(value),
              let v = try? JSONDecoder().decode(JSONValue.self, from: data) else { return nil }
        self = v
    }
}

extension JSONValue: ExpressibleByStringLiteral, ExpressibleByIntegerLiteral, ExpressibleByFloatLiteral,
    ExpressibleByBooleanLiteral, ExpressibleByDictionaryLiteral, ExpressibleByArrayLiteral {
    init(stringLiteral value: String) { self = .string(value) }
    init(integerLiteral value: Int) { self = .number(Double(value)) }
    init(floatLiteral value: Double) { self = .number(value) }
    init(booleanLiteral value: Bool) { self = .bool(value) }
    init(dictionaryLiteral elements: (String, JSONValue)...) {
        var o: [String: JSONValue] = [:]
        for (k, v) in elements { o[k] = v }
        self = .object(o)
    }
    init(arrayLiteral elements: JSONValue...) { self = .array(elements) }
}
