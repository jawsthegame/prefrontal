import CoreImage.CIFilterBuiltins
import UIKit

/// Renders a string (here, a `prefrontal://connect?…` link) as a QR `UIImage`.
///
/// The operator admin screen shows this so a freshly-provisioned user can point
/// their new phone's camera at it and "Open in Prefrontal" — the same handoff
/// the CLI's `prefrontal user connect-link --qr` prints on the paper setup
/// sheet, but on-screen. iOS Camera recognises the custom scheme in the code.
enum QRCode {
    /// Shared render context — a `CIContext` is relatively expensive to build, and
    /// this helper can be re-invoked on SwiftUI re-renders, so reuse one.
    private static let context = CIContext()

    /// A crisp opaque QR for `string`, or `nil` if it can't be encoded (empty, or
    /// too much data for a single symbol). The CoreImage generator emits a tiny
    /// 1-module-per-pixel image; we scale it up with nearest-neighbour so the
    /// squares stay hard-edged rather than blurring when SwiftUI resizes it.
    static func image(from string: String, scale: CGFloat = 12) -> UIImage? {
        guard !string.isEmpty else { return nil }
        let filter = CIFilter.qrCodeGenerator()
        filter.message = Data(string.utf8)
        filter.correctionLevel = "M"  // ~15% recovery — a good balance for a screen scan
        guard let output = filter.outputImage else { return nil }
        let scaled = output.transformed(by: CGAffineTransform(scaleX: scale, y: scale))
        guard let cg = context.createCGImage(scaled, from: scaled.extent) else { return nil }
        return UIImage(cgImage: cg)
    }
}
