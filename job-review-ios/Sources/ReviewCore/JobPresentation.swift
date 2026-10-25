import Foundation

public enum AssessmentText {
    /// Format existing assessment text without shortening or inventing evidence.
    public static func bulletPoints(from text: String) -> [String] {
        text.components(separatedBy: .newlines).flatMap { line -> [String] in
            let content = line.trimmingCharacters(in: .whitespaces)
                .replacingOccurrences(of: "^(?:[-*•‣]\\s+|[0-9]+[.)]\\s+)", with: "", options: .regularExpression)
                .trimmingCharacters(in: .whitespaces)
            guard !content.isEmpty else { return [] }
            var points: [String] = []
            content.enumerateSubstrings(in: content.startIndex..<content.endIndex, options: .bySentences) { sentence, _, _, _ in
                if let point = sentence?.trimmingCharacters(in: .whitespacesAndNewlines), !point.isEmpty {
                    points.append(point)
                }
            }
            return points.isEmpty ? [content] : points
        }
    }
}

public extension Job {
    /// Preserve the posting's requirements; fit and gap notes are not requirements.
    var requirements: [String] {
        switch data["requirements"] {
        case .string(let text):
            return nonblankJobText(.string(text)).map { [$0] } ?? []
        case .array(let values):
            return values.compactMap(nonblankJobText)
        default:
            return []
        }
    }

    var payDescription: String {
        nonblankJobText(data["salary"]) ?? "Not stated"
    }

    var workSettingDescription: String {
        nonblankJobText(data["arrangement"]) ?? "Not stated"
    }

    /// Keep source gaps separate from both requirements and the ranker's explanation.
    var sourceGaps: String? {
        if case .array(let values) = data["gaps"] {
            let gaps = values.compactMap(nonblankJobText)
            return gaps.isEmpty ? nil : gaps.joined(separator: "\n")
        }
        return nonblankJobText(data["gaps"])
    }

    var roleDescription: String? {
        nonblankJobText(data["description"])
    }
}

private func nonblankJobText(_ value: JSONValue?) -> String? {
    guard let text = value?.text?.trimmingCharacters(in: .whitespacesAndNewlines), !text.isEmpty else {
        return nil
    }
    return text
}
