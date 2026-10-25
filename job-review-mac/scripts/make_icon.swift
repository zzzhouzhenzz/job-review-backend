import AppKit
import Foundation
let output = URL(fileURLWithPath: CommandLine.arguments[1])
try FileManager.default.createDirectory(at: output, withIntermediateDirectories: true)
for size in [16, 32, 128, 256, 512] {
    for scale in [1, 2] {
        let pixels = size * scale
        let image = NSImage(size: NSSize(width: 1024, height: 1024))
        image.lockFocus()
        NSColor(calibratedRed: 0.91, green: 0.93, blue: 0.90, alpha: 1).setFill()
        NSBezierPath(roundedRect: NSRect(x: 80, y: 80, width: 864, height: 864), xRadius: 194, yRadius: 194).fill()
        for (offset, shade) in [(100.0, 0.65), (50.0, 0.48), (0.0, 0.31)] {
            NSColor(calibratedRed: shade, green: shade + 0.12, blue: shade + 0.07, alpha: 1).setFill()
            NSBezierPath(roundedRect: NSRect(x: 260 - offset / 2, y: 215 + offset, width: 510, height: 500), xRadius: 48, yRadius: 48).fill()
        }
        NSColor(calibratedRed: 0.94, green: 0.95, blue: 0.91, alpha: 1).setStroke()
        let check = NSBezierPath()
        check.lineWidth = 39; check.lineCapStyle = .round; check.lineJoinStyle = .round
        check.move(to: NSPoint(x: 378, y: 458)); check.line(to: NSPoint(x: 468, y: 366)); check.line(to: NSPoint(x: 638, y: 552)); check.stroke()
        image.unlockFocus()
        let bitmap = NSBitmapImageRep(bitmapDataPlanes: nil, pixelsWide: pixels, pixelsHigh: pixels, bitsPerSample: 8, samplesPerPixel: 4, hasAlpha: true, isPlanar: false, colorSpaceName: .deviceRGB, bytesPerRow: 0, bitsPerPixel: 0)!
        let context = NSGraphicsContext(bitmapImageRep: bitmap)!
        NSGraphicsContext.saveGraphicsState(); NSGraphicsContext.current = context
        image.draw(in: NSRect(x: 0, y: 0, width: pixels, height: pixels))
        NSGraphicsContext.restoreGraphicsState()
        let name = "icon_\(size)x\(size)\(scale == 2 ? "@2x" : "").png"
        try bitmap.representation(using: .png, properties: [:])!.write(to: output.appendingPathComponent(name))
    }
}
