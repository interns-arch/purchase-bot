import { Navigate, Route, BrowserRouter as Router, Routes } from "react-router-dom";
import { AuthProvider } from "./context/AuthContext";
import { ToastProvider } from "./context/ToastContext";
import { ProtectedRoute } from "./components/ProtectedRoute";
import { LoginPage } from "./pages/LoginPage";
import { DashboardPage } from "./pages/DashboardPage";
import { VendorsPage } from "./pages/VendorsPage";
import { VendorInventoryPage } from "./pages/VendorInventoryPage";
import { CustomerOrdersPage } from "./pages/CustomerOrdersPage";
import { VendorComparisonPage } from "./pages/VendorComparisonPage";
import { SettingsPage } from "./pages/SettingsPage";
import { ComingSoonPage } from "./pages/ComingSoonPage";

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
              path="/vendors"
              element={
                <ProtectedRoute>
                  <VendorsPage />
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
              path="/purchase-orders"
              element={
                <ProtectedRoute>
                  <ComingSoonPage
                    title="Purchase Orders"
                    description="Once the purchase team selects vendors from the comparison report, purchase orders will be generated here automatically."
                  />
                </ProtectedRoute>
              }
            />
            <Route
              path="/delivery-upload"
              element={
                <ProtectedRoute>
                  <ComingSoonPage
                    title="Delivery Upload"
                    description="Vendor delivery files will be uploaded here to track ordered vs. delivered quantities."
                  />
                </ProtectedRoute>
              }
            />
            <Route
              path="/vendor-performance"
              element={
                <ProtectedRoute>
                  <ComingSoonPage
                    title="Vendor Performance"
                    description="Vendor reliability (delivered vs. ordered accuracy) will be tracked here once deliveries are recorded."
                  />
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
            <Route path="/" element={<Navigate to="/dashboard" replace />} />
            <Route path="*" element={<Navigate to="/dashboard" replace />} />
          </Routes>
        </AuthProvider>
      </ToastProvider>
    </Router>
  );
}

export default App;
