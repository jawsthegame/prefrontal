import SwiftUI
import AVFoundation
import AudioToolbox

/// A live camera QR scanner. Reports the first decoded string back via
/// `onScan` (the caller decides whether it's a valid `ConnectPayload`), and
/// surfaces camera-permission / hardware failures via `onError` rather than
/// showing a dead black rectangle.
///
/// Wrapped as a `UIViewControllerRepresentable` because `AVCaptureSession`
/// has no first-class SwiftUI equivalent. Capture is torn down in
/// `viewWillDisappear`, so dismissing the sheet stops the camera.
struct QRScannerView: UIViewControllerRepresentable {
    var onScan: (String) -> Void
    var onError: (String) -> Void

    func makeUIViewController(context: Context) -> ScannerController {
        let c = ScannerController()
        c.onScan = onScan
        c.onError = onError
        return c
    }

    func updateUIViewController(_ controller: ScannerController, context: Context) {}

    final class ScannerController: UIViewController, AVCaptureMetadataOutputObjectsDelegate {
        var onScan: ((String) -> Void)?
        var onError: ((String) -> Void)?

        private let session = AVCaptureSession()
        private var preview: AVCaptureVideoPreviewLayer?
        private var handled = false   // fire onScan exactly once

        override func viewDidLoad() {
            super.viewDidLoad()
            view.backgroundColor = .black
            switch AVCaptureDevice.authorizationStatus(for: .video) {
            case .authorized:
                configure()
            case .notDetermined:
                AVCaptureDevice.requestAccess(for: .video) { [weak self] granted in
                    DispatchQueue.main.async {
                        granted ? self?.configure()
                                : self?.onError?("Camera access is off. Enter your details by hand instead.")
                    }
                }
            default:
                onError?("Camera access is off — enable it in Settings, or enter your details by hand.")
            }
        }

        private func configure() {
            guard let device = AVCaptureDevice.default(for: .video),
                  let input = try? AVCaptureDeviceInput(device: device),
                  session.canAddInput(input) else {
                onError?("This device has no usable camera. Enter your details by hand instead.")
                return
            }
            session.addInput(input)

            let output = AVCaptureMetadataOutput()
            guard session.canAddOutput(output) else {
                onError?("Couldn't start the scanner. Enter your details by hand instead.")
                return
            }
            session.addOutput(output)
            output.setMetadataObjectsDelegate(self, queue: .main)
            output.metadataObjectTypes = [.qr]

            let layer = AVCaptureVideoPreviewLayer(session: session)
            layer.videoGravity = .resizeAspectFill
            layer.frame = view.layer.bounds
            view.layer.addSublayer(layer)
            preview = layer

            // Capture I/O off the main thread — startRunning blocks otherwise.
            DispatchQueue.global(qos: .userInitiated).async { [weak self] in
                self?.session.startRunning()
            }
        }

        override func viewDidLayoutSubviews() {
            super.viewDidLayoutSubviews()
            preview?.frame = view.layer.bounds
        }

        override func viewWillDisappear(_ animated: Bool) {
            super.viewWillDisappear(animated)
            if session.isRunning {
                DispatchQueue.global(qos: .userInitiated).async { [weak self] in
                    self?.session.stopRunning()
                }
            }
        }

        func metadataOutput(_ output: AVCaptureMetadataOutput,
                            didOutput objects: [AVMetadataObject],
                            from connection: AVCaptureConnection) {
            guard !handled,
                  let obj = objects.first as? AVMetadataMachineReadableCodeObject,
                  let value = obj.stringValue else { return }
            handled = true
            AudioServicesPlaySystemSound(kSystemSoundID_Vibrate)
            onScan?(value)
        }
    }
}
