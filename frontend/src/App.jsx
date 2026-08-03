import { Navigate, Route, BrowserRouter as Router, Routes } from "react-router-dom";
import { AuthProvider } from "./context/AuthContext";
import { ToastProvider } from "./context/ToastContext";
import { ProtectedRoute } from "./components/ProtectedRoute";
import { LoginPage } from "./pages/LoginPage";
import { DashboardPage } from "./pages/DashboardPage";
import { VendorInventoryPage } from "./pages/VendorInventoryPage";
import { CustomerOrdersPage } from "./pages/CustomerOrdersPage";
import { VendorComparisonPage } from "./pages/VendorComparisonPage";
import { DocumentInboxPage } from "./pages/DocumentInboxPage";
import { SettingsPage } from "./pages/SettingsPage";
import { IntegrationStatusPage } from "./pages/IntegrationStatusPage";
import { DeliveryTrackingPage } from "./pages/DeliveryTrackingPage";
import { VendorPerformancePage } from "./pages/VendorPerformancePage";
import { VendorPerformanceDetailPage } from "./pages/VendorPerformanceDetailPage";

function App() {
  return (
    <Router>
      <ToastProvider>
        <AuthProvider>
          <Routes>
            <Route path="/login" element={<LoginPage />} />
            <Route
              path="/dashboard"
              element={
                <ProtectedRoute>
                  <DashboardPage />
                </ProtectedRoute>
              }
            />
            <Route
              path="/vendor-inventory"
              element={
                <ProtectedRoute>
                  <VendorInventoryPage />
                </ProtectedRoute>
              }
            />
            <Route
              path="/customer-orders"
              element={
                <ProtectedRoute>
                  <CustomerOrdersPage />
                </ProtectedRoute>
              }
            />
            <Route
              path="/vendor-comparison"
              element={
                <ProtectedRoute>
                  <VendorComparisonPage />
                </ProtectedRoute>
              }
            />
            <Route
              path="/delivery-tracking"
              element={
                <ProtectedRoute>
                  <DeliveryTrackingPage />
                </ProtectedRoute>
              }
            />
            <Route
              path="/vendor-performance"
              element={
                <ProtectedRoute>
                  <VendorPerformancePage />
                </ProtectedRoute>
              }
            />
            <Route
              path="/vendor-performance/:vendorId"
              element={
                <ProtectedRoute>
                  <VendorPerformanceDetailPage />
                </ProtectedRoute>
              }
            />
            <Route
              path="/document-inbox"
              element={
                <ProtectedRoute>
                  <DocumentInboxPage />
                </ProtectedRoute>
              }
            />
            <Route
              path="/settings"
              element={
                <ProtectedRoute>
                  <SettingsPage />
                </ProtectedRoute>
              }
            />
            <Route
              path="/settings/integrations"
              element={
                <ProtectedRoute>
                  <IntegrationStatusPage />
                </ProtectedRoute>
              }
            />
            <Route path="/" element={<Navigate to="/dashboard" replace />} />
            <Route path="*" element={<Navigate to="/dashboard" replace />} />
          </Routes>
        </AuthProvider>
      </ToastProvider>
    </Router>
  );
}

export default App;
