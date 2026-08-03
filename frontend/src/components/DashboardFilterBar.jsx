import { useEffect, useState } from "react";
import { listCustomerOrders } from "../api/customerOrders";

function FilterField({ label, width, children }) {
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 4, maxWidth: width }}>
      <span style={{ fontSize: "0.75rem", color: "var(--color-text-muted)", fontWeight: 600 }}>
        {label}
      </span>
      {children}
    </div>
  );
}

export function DashboardFilterBar({ filters, onChange, vendorOptions, statusOptions }) {
  const [orders, setOrders] = useState([]);

  useEffect(() => {
    listCustomerOrders()
      .catch(() => [])
      .then((data) => setOrders(data || []));
  }, []);

  function update(patch) {
    onChange({ ...filters, ...patch });
  }

  return (
    <div className="toolbar">
      <FilterField label="From" width={150}>
        <input
          type="date"
          className="field__input"
          value={filters.dateFrom || ""}
          onChange={(event) => update({ dateFrom: event.target.value })}
        />
      </FilterField>
      <FilterField label="To" width={150}>
        <input
          type="date"
          className="field__input"
          value={filters.dateTo || ""}
          onChange={(event) => update({ dateTo: event.target.value })}
        />
      </FilterField>
      <FilterField label="Vendor" width={200}>
        <select
          className="field__input"
          value={filters.vendorId || ""}
          onChange={(event) => update({ vendorId: event.target.value })}
        >
          <option value="">All vendors</option>
          {(vendorOptions || []).map((vendor) => (
            <option key={vendor.id} value={vendor.id}>
              {vendor.name}
            </option>
          ))}
        </select>
      </FilterField>
      <FilterField label="Customer Order" width={220}>
        <select
          className="field__input"
          value={filters.customerOrderId || ""}
          onChange={(event) => update({ customerOrderId: event.target.value })}
        >
          <option value="">All orders</option>
          {orders.map((order) => (
            <option key={order.id} value={order.id}>
              {order.file_name}
            </option>
          ))}
        </select>
      </FilterField>
      <FilterField label="Part Number" width={200}>
        <input
          className="field__input"
          type="search"
          placeholder="Search part…"
          value={filters.partNumber || ""}
          onChange={(event) => update({ partNumber: event.target.value })}
        />
      </FilterField>
      {statusOptions && (
        <FilterField label="Status" width={180}>
          <select
            className="field__input"
            value={filters.status || ""}
            onChange={(event) => update({ status: event.target.value })}
          >
            <option value="">All statuses</option>
            {statusOptions.map((option) => (
              <option key={option} value={option}>
                {option.replaceAll("_", " ")}
              </option>
            ))}
          </select>
        </FilterField>
      )}
      <button type="button" className="btn btn--ghost" onClick={() => onChange({})}>
        Clear Filters
      </button>
    </div>
  );
}
