import { Layout } from "../components/Layout";
import { EmptyState } from "../components/EmptyState";

export function ComingSoonPage({ title, description }) {
  return (
    <Layout title={title}>
      <section className="panel">
        <EmptyState
          title={`${title} is coming in a later phase`}
          description={description}
        />
      </section>
    </Layout>
  );
}
