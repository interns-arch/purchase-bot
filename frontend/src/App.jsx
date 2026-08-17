import { lazy, Suspense } from "react";
import { Navigate, Route, BrowserRouter as Router, Routes } from "react-router-dom";
import { AuthProvider } from "./context/AuthContext";
import { ToastProvider } from "./context/ToastContext";
import { ProtectedRoute } from "./components/ProtectedRoute";
import { IntegrationNotifications } from "./components/IntegrationNotifications";

// Lazy-loaded so each page is its own chunk instead of one large bundle.
const LoginPage = lazy(() => import("./pages/LoginPage").then((m) => ({ default: m.LoginPage })));
const DashboardPage = lazy(() =>
  import("./pages/DashboardPage").then((m) => ({ default: m.DashboardPage }))
);
const VendorInventoryPage = lazy(() =>
  import("./pages/VendorInventoryPage").then((m) => ({ default: m.VendorInventoryPage }))
);
const CustomerOrdersPage = lazy(() =>
  import("./pages/CustomerOrdersPage").then((m) => ({ default: m.CustomerOrdersPage }))
);
const VendorComparisonPage = lazy(() =>
  import("./pages/VendorComparisonPage").then((m) => ({ default: m.VendorComparisonPage }))
);
const SettingsPage = lazy(() =>
  import("./pages/SettingsPage").then((m) => ({ default: m.SettingsPage }))
);
const IntegrationStatusPage = lazy(() =>
  import("./pages/IntegrationStatusPage").then((m) => ({ default: m.IntegrationStatusPage }))
);
const DeliveryTrackingPage = lazy(() =>
  import("./pages/DeliveryTrackingPage").then((m) => ({ default: m.DeliveryTrackingPage }))
);
const VendorInvoicesPage = lazy(() =>
  import("./pages/VendorInvoicesPage").then((m) => ({ default: m.VendorInvoicesPage }))
);
const PurchaseOrdersPage = lazy(() =>
  import("./pages/PurchaseOrdersPage").then((m) => ({ default: m.PurchaseOrdersPage }))
);
const VendorPerformancePage = lazy(() =>
  import("./pages/VendorPerformancePage").then((m) => ({ default: m.VendorPerformancePage }))
);
const VendorPerformanceDetailPage = lazy(() =>
  import("./pages/VendorPerformanceDetailPage").then((m) => ({
    default: m.VendorPerformanceDetailPage,
  }))
);
const FileInboxPage = lazy(() =>
  import("./pages/FileInboxPage").then((m) => ({ default: m.FileInboxPage }))
);
const CommandCentrePage = lazy(() =>
  import("./pages/CommandCentrePage").then((m) => ({ default: m.CommandCentrePage }))
);

function App() {
  return (
    <Router>
      <ToastProvider>
        <AuthProvider>
          <IntegrationNotifications />
          <Suspense fallback={null}>
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
              path="/vendor-invoices"
              element={
                <ProtectedRoute>
                  <VendorInvoicesPage />
                </ProtectedRoute>
              }
            />
            <Route
              path="/purchase-orders"
              element={
                <ProtectedRoute>
                  <PurchaseOrdersPage />
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
              path="/command-centre"
              element={
                <ProtectedRoute>
                  <CommandCentrePage />
                </ProtectedRoute>
              }
            />
            <Route
              path="/file-inbox"
              element={
                <ProtectedRoute>
                  <FileInboxPage />
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
          </Suspense>
        </AuthProvider>
      </ToastProvider>
    </Router>
  );
}

export default App;
