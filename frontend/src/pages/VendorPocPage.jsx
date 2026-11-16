import { Layout } from "../components/Layout";
import { VendorPocPanel } from "./VendorPocPanel";

// POC tab in the main menu (User, 14 Nov 2026).
export function VendorPocPage() {
  return (
    <Layout title="Vendor POC">
      <VendorPocPanel />
    </Layout>
  );
}
