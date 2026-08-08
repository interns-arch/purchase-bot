"""Customer lookup/creation. Mirrors `vendor_service.py`: there is no manual
customer management in this application -- customers are only ever
auto-created from an imported Customer Order file's name (see
`document_processor.dispatcher`). Pure business logic -- no
print()/input() here."""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from core.models import Customer


def create_customer(name: str, session: Session) -> Customer:
    name = name.strip()
    if not name:
        raise ValueError("Customer name cannot be blank.")

    if get_customer_by_name(name, session) is not None:
        raise ValueError(f"A customer named '{name}' already exists.")

    customer = Customer(name=name)
    session.add(customer)
    session.flush()  # assign customer.id
    return customer


def get_customer(customer_id: int, session: Session) -> Customer | None:
    return session.get(Customer, customer_id)


def get_customer_by_name(name: str, session: Session) -> Customer | None:
    return session.execute(
        select(Customer).where(func.lower(Customer.name) == name.strip().lower())
    ).scalar_one_or_none()
