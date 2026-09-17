"""Advance orders from the Cartrends sales bot (AutoFlow), 16 Sep 2026.

The sales bot punches what is in stock; for the rest the customer may ask for
an advance order by a date. That request arrives here. ProcureHub asks the
vendors for each brand ONE BY ONE on WhatsApp, reads their replies (yes/no,
quantity, ETA), and the sales bot polls the result and tells the customer.
When the customer confirms, the vendors who said yes get the order and the
admins / purchase team are told.

Switched OFF unless ADVANCE_ORDERS_ENABLED=true -- every API call answers 503
and no vendor is ever messaged. See `config.py`.
"""
