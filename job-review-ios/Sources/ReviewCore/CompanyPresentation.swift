import Foundation

public extension Company {
    /// Company facts are separate from the ranker's explanation of selected jobs.
    var businessDescription: String? {
        guard let raw = data["description"]?.text else { return nil }
        let text = raw.split(whereSeparator: { $0.isWhitespace }).joined(separator: " ")
        guard !text.isEmpty else { return nil }
        var sentences: [String] = []
        text.enumerateSubstrings(in: text.startIndex..<text.endIndex, options: .bySentences) { sentence, _, _, stop in
            if let sentence {
                let trimmed = sentence.trimmingCharacters(in: .whitespacesAndNewlines)
                if !trimmed.isEmpty { sentences.append(trimmed) }
            }
            stop = sentences.count == 2
        }
        return sentences.isEmpty ? text : sentences.joined(separator: " ")
    }

    /// A logo must be supplied explicitly. A job-board host is not company identity.
    var logoURL: URL? {
        guard let raw = data["logo_url"]?.text,
              let components = URLComponents(string: raw.trimmingCharacters(in: .whitespacesAndNewlines)),
              components.scheme?.lowercased() == "https",
              components.user == nil, components.password == nil,
              let host = components.host, !host.isEmpty,
              !host.unicodeScalars.contains(where: { CharacterSet.whitespacesAndNewlines.contains($0) }),
              let url = components.url else { return nil }
        return url
    }

    /// Keep the fallback compact without splitting an extended Unicode character.
    var displayInitials: String {
        let initials = name.split(whereSeparator: { $0.isWhitespace })
            .prefix(2).compactMap(\.first).map(String.init).joined().uppercased()
        return initials.isEmpty ? "?" : String(initials.prefix(2))
    }
}
