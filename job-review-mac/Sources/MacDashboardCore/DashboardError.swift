import Foundation

/// Public errors contain no response bodies, URLs, credentials, or underlying error text.
public enum DashboardError: Error, LocalizedError, Equatable, Sendable {
    case configuration
    case authentication
    case http(Int)
    case tls
    case network
    case timeout
    case invalidResponse

    public var errorDescription: String? {
        switch self {
        case .configuration: return "The connection settings or private credential file could not be read."
        case .authentication: return "The service rejected the saved credential."
        case .http(let status): return "The service returned HTTP \(status)."
        case .tls: return "The secure connection could not be verified."
        case .network: return "The service could not be reached."
        case .timeout: return "The service did not respond in time."
        case .invalidResponse: return "The service returned an incomplete or unsupported response."
        }
    }

    public var recoverySuggestion: String? {
        switch self {
        case .configuration:
            return "Check the connection file and credential location. Private files must belong to your account, use mode 0600, and must not be symbolic links."
        case .authentication:
            return "Check the saved credential and its service address, then refresh."
        case .http(let status) where (300...399).contains(status):
            return "Redirects are blocked. Use the service's exact HTTPS address."
        case .http:
            return "Refresh later. If this continues, inspect the backend's local diagnostics."
        case .tls:
            return "Check Tailscale and the service certificate. Certificate checks remain enabled."
        case .network, .timeout:
            return "Check that Tailscale is connected and the backend Mac is awake, then refresh."
        case .invalidResponse:
            return "Refresh again. If this continues, check that the backend supports the complete v1 API."
        }
    }

    static func safe(_ error: Error) -> Error {
        if error is CancellationError { return error }
        if let error = error as? DashboardError { return error }
        if let error = error as? URLError {
            switch error.code {
            case .cancelled: return CancellationError()
            case .timedOut: return DashboardError.timeout
            case .secureConnectionFailed, .serverCertificateHasBadDate, .serverCertificateUntrusted,
                 .serverCertificateHasUnknownRoot, .serverCertificateNotYetValid,
                 .clientCertificateRejected, .clientCertificateRequired:
                return DashboardError.tls
            default: return DashboardError.network
            }
        }
        return DashboardError.invalidResponse
    }
}
