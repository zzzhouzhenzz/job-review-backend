import Foundation

public enum SwipeDecision {
    /// Distance, not velocity, commits. Vertical scrolling and short drags never decide.
    public static func action(horizontal: Double, vertical: Double, cardWidth: Double) -> CardAction? {
        let threshold = max(90, min(140, cardWidth * 0.28))
        guard abs(horizontal) >= threshold, abs(horizontal) > abs(vertical) * 1.7 else { return nil }
        return horizontal > 0 ? .approve : .exclude
    }
}
