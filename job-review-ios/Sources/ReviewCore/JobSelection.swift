/// An unsaved selection of exact job versions. New or changed jobs need a new choice.
public struct JobSelection: Equatable, Sendable {
    private var revisions: [String: Int] = [:]

    public init() {}

    public var keys: Set<String> { Set(revisions.keys) }

    public mutating func toggle(_ job: Job) {
        if revisions[job.key] == job.revision {
            revisions.removeValue(forKey: job.key)
        } else {
            revisions[job.key] = job.revision
        }
    }

    public mutating func clear() { revisions.removeAll() }

    /// Pass the full Ready list, not search results. Never select newly available versions.
    @discardableResult
    public mutating func reconcile(with readyJobs: [Job]) -> Int {
        let previousCount = revisions.count
        revisions = revisions.filter { key, revision in
            readyJobs.contains { $0.key == key && $0.revision == revision }
        }
        return previousCount - revisions.count
    }
}
