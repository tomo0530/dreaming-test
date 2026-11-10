"""Seed-deterministic generator of SchemaDrift-SQL streams.

A stream is built in four layers, each derived only from the seed:

1. Records (`_World`): customers, products and orders with a full event timeline
   (payment, shipment, delivery, return, cancellation, voiding).
2. A design (`_Design`): obfuscated physical names, code values, units, time formats
   and the two migrations. It yields one physical schema per epoch (`_Schema`).
3. One SQLite file per epoch, rendered from the records as of the epoch's cutoff.
4. Questions: business-language text, a gold SQL per epoch built from the schema,
   and expected answers computed from the records in Python. Expected answers never
   come from the gold SQL, so `validate_stream` can catch a wrong gold query or a
   wrong rendering.

Each migration changes one rule that is visible in the DDL (a renamed column, a new
join key, a replacement table that leaves the old one stale) and one that is not (a
unit change, reassigned status codes, voided orders kept as flagged rows).
"""

import calendar
import json
import logging
import random
import sqlite3
from abc import ABC, abstractmethod
from bisect import bisect_left
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import date, datetime, time, timedelta
from enum import StrEnum
from itertools import accumulate
from pathlib import Path
from typing import Any, Literal

from project_module.schemadrift.grader import Rows, results_equal, run_for_grading
from project_module.schemadrift.models import (
    SESSIONS_PER_EPOCH,
    Fact,
    FactKind,
    InjectedNote,
    Question,
    Stream,
)
from project_module.schemadrift.sql import SqlError
from project_module.schemadrift.validate import (
    INJECTION_AFTER_SESSIONS,
    INJECTION_WINDOW,
    epoch_start,
)

_log = logging.getLogger(__name__)

STREAM_FILE = "stream.json"

Unit = Literal["cents", "dollars"]
TimeFormat = Literal["unix", "iso", "yyyymmdd"]
TIME_FORMAT_LABELS: Mapping[TimeFormat, str] = {
    "unix": "unix epoch seconds",
    "iso": "ISO text YYYY-MM-DD HH:MM:SS",
    "yyyymmdd": "integer YYYYMMDD",
}

# Data runs through 2025; each epoch adds four months. Cutoffs are exclusive.
_EPOCH_CUTOFFS: Mapping[int, datetime] = {
    1: datetime(2025, 5, 1),
    2: datetime(2025, 9, 1),
    3: datetime(2026, 1, 1),
}
_MAX_SESSIONS = len(_EPOCH_CUTOFFS) * SESSIONS_PER_EPOCH
_MIN_QUESTIONS_PER_SESSION = 3

# World size and order outcome rates.
_CUSTOMER_COUNT = 360
_CATEGORY_COUNT = 6
_PRODUCTS_PER_CATEGORY = 15
_CARRIER_WEIGHTS = (50, 32, 18)
_ORDERS_PER_DAY = (3, 7)
_VOID_RATE = 0.04
_CANCEL_RATE = 0.07
_RETURN_RATE = 0.09

_LEAF_PRODUCT = "leaf.product_discontinued"
_LEAF_CUSTOMER = "leaf.customer_business"


# ---------------------------------------------------------------------------
# Domain vocabulary
# ---------------------------------------------------------------------------


class _Status(StrEnum):
    PENDING = "Pending payment"
    PAID = "Paid"
    SHIPPED = "Shipped"
    DELIVERED = "Delivered"
    CANCELLED = "Cancelled"
    RETURNED = "Returned"


class _Channel(StrEnum):
    WEB = "Web storefront"
    MOBILE = "Mobile app"
    MARKETPLACE = "Partner marketplace"
    PHONE = "Phone order"


class _Segment(StrEnum):
    CONSUMER = "Consumer"
    BUSINESS = "Business account"
    EMPLOYEE = "Employee"


class _ProductState(StrEnum):
    ACTIVE = "Active"
    DISCONTINUED = "Discontinued"
    PREORDER = "Pre-order"


# Statuses that questions filter on, and therefore have a fact each.
_FACT_STATUSES = (_Status.DELIVERED, _Status.CANCELLED, _Status.RETURNED)

_REGIONS = ("North", "South", "East", "West", "Central")
_CARRIERS = ("Swiftline", "Parcelnet", "Bluejay Express", "Northstar Freight", "Kestrel Post")
_CATEGORIES: Mapping[str, tuple[tuple[int, int], tuple[str, ...]]] = {
    "Kitchen": (
        (8, 120),
        ("Skillet", "Kettle", "Knife Set", "Colander", "Cutting Board", "Mixing Bowl"),
    ),
    "Garden": (
        (6, 150),
        ("Trowel", "Hose Reel", "Planter", "Pruner", "Seed Kit", "Watering Can"),
    ),
    "Toys": ((5, 80), ("Puzzle", "Robot Kit", "Plush Bear", "Kite", "Block Set", "Race Car")),
    "Books": (
        (6, 45),
        ("Cookbook", "Atlas", "Novel", "Field Guide", "Journal", "Sketchbook"),
    ),
    "Electronics": (
        (15, 240),
        ("Headphones", "Charger", "Speaker", "Smart Plug", "Webcam", "Power Bank"),
    ),
    "Outdoor": ((10, 200), ("Tent", "Lantern", "Backpack", "Hammock", "Camp Stove", "Cooler")),
    "Beauty": (
        (5, 60),
        ("Face Serum", "Hair Dryer", "Lip Balm Set", "Hand Cream", "Nail Kit", "Shaving Kit"),
    ),
    "Office": (
        (3, 90),
        ("Desk Lamp", "Notebook Set", "Stapler", "Monitor Stand", "Pen Set", "File Organizer"),
    ),
    "Pets": ((4, 70), ("Dog Bed", "Cat Tree", "Leash", "Pet Bowl", "Chew Toy", "Litter Mat")),
    "Sports": (
        (8, 160),
        (
            "Yoga Mat",
            "Dumbbell Set",
            "Jump Rope",
            "Water Bottle",
            "Tennis Racket",
            "Cycling Gloves",
        ),
    ),
}
_ADJECTIVES = (
    "Classic", "Compact", "Deluxe", "Essential", "Everyday", "Premium", "Rustic", "Smart",
    "Sturdy", "Urban", "Vintage", "Nimble", "Bright", "Coastal", "Alpine", "Studio",
)  # fmt: skip
_FIRST_NAMES = (
    "Ava", "Liam", "Noah", "Emma", "Olivia", "Mason", "Sofia", "Lucas", "Mia", "Ethan",
    "Amelia", "James", "Harper", "Logan", "Ella", "Aiden", "Chloe", "Owen", "Grace", "Caleb",
    "Nora", "Henry", "Lily", "Isaac", "Zoe", "Julian", "Hannah", "Leo", "Ruby", "Miles",
    "Iris", "Felix", "Clara", "Hugo", "Maya", "Theo", "Alice", "Jonah", "Eva", "Simon",
)  # fmt: skip
_LAST_NAMES = (
    "Abbott", "Barnes", "Carver", "Dalton", "Ellison", "Fletcher", "Garner", "Hayes", "Ingram",
    "Jensen", "Keller", "Lawson", "Mercer", "Nolan", "Osborne", "Porter", "Quinn", "Rhodes",
    "Sutton", "Tate", "Upton", "Vance", "Walsh", "Yates", "Ashby", "Bennett", "Crane", "Draper",
    "Easton", "Foley", "Griffin", "Holt", "Irwin", "Jarvis", "Kirby", "Lowell", "Marsh",
    "Norris", "Pruitt", "Reyes",
)  # fmt: skip
_MONTH_NAMES = tuple(calendar.month_name[m] for m in range(1, 13))

# Candidate physical names; one is drawn per slot. `*_v2` names appear after a migration.
_NAME_CHOICES: Mapping[str, tuple[str, str, str]] = {
    "customers": ("customers", "cust_master", "clients"),
    "c_pk": ("cid", "cust_no", "client_key"),
    "c_name": ("full_name", "cust_nm", "display_name"),
    "c_region": ("rgn", "zone_cd", "area_cd"),
    "c_region_v2": ("sales_region", "geo_zone", "territory"),
    "c_signup": ("signup_dt", "reg_date", "created_on"),
    "c_segment": ("seg_cd", "acct_cls", "cust_type"),
    "c_acct": ("acct_no", "account_ref", "acct_key"),
    "products": ("products", "catalog_items", "prod_master"),
    "p_pk": ("sku_id", "prod_no", "item_key"),
    "p_name": ("prod_nm", "item_title", "item_name"),
    "p_category": ("cat", "dept", "grp_nm"),
    "p_price": ("list_px", "base_price", "px_list"),
    "p_state": ("lc_cd", "state_cd", "lifecycle_cd"),
    "orders": ("orders", "sales_orders", "ord_hdr"),
    "o_pk": ("ord_id", "order_no", "oid"),
    "o_cust": ("buyer_ref", "cust_key", "party_id"),
    "o_cust_v2": ("acct_ref", "account_id", "acct_fk"),
    "o_placed": ("plc_ts", "created_at", "ord_dt"),
    "o_placed_v2": ("placed_on", "booking_ts", "entry_time"),
    "o_status": ("st", "status_cd", "ord_state"),
    "o_channel": ("chn", "src_cd", "channel_cd"),
    "o_amount": ("amt", "total_amt", "gross_amt"),
    "o_void": ("void_flg", "is_void", "del_ind"),
    "lines": ("order_lines", "ord_items", "sales_lines"),
    "l_pk": ("line_id", "ln_no", "item_seq"),
    "l_order": ("hdr_ref", "ord_ref", "parent_no"),
    "l_product": ("sku_ref", "prod_ref", "item_ref"),
    "l_qty": ("qty", "n_units", "qty_ord"),
    "l_price": ("unit_px", "line_price", "px_each"),
    "ref": ("ref_codes", "code_lookup", "lkp_values"),
    "r_domain": ("domain", "code_set", "lkp_type"),
    "r_code": ("code", "cd", "code_val"),
    "r_label": ("label", "meaning", "descr"),
    "ship": ("shipments", "shp_log", "dispatches"),
    "s_pk": ("shp_id", "ship_no", "dispatch_id"),
    "s_order": ("ord_ref", "order_key", "so_ref"),
    "s_carrier": ("carrier_nm", "courier_name", "svc_provider"),
    "s_shipped": ("shipped_ts", "disp_time", "out_ts"),
    "s_delivered": ("delivered_ts", "pod_time", "dlv_ts"),
    "ship_v2": ("fulfilment_events", "shipments_v2", "parcel_tracking"),
    "s2_pk": ("evt_id", "tracking_id", "fe_id"),
    "s2_order": ("order_ref", "so_number", "ord_key"),
    "s2_carrier": ("carrier_name", "courier_nm", "provider_nm"),
    "s2_shipped": ("dispatched_at", "left_dc_ts", "handover_ts"),
    "s2_delivered": ("delivered_at", "received_ts", "pod_ts"),
}


# ---------------------------------------------------------------------------
# Records
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Customer:
    cid: int
    acct_no: int
    name: str
    region: str
    signup: date
    segment: _Segment


@dataclass(frozen=True)
class _Product:
    pid: int
    name: str
    category: str
    price_cents: int
    state: _ProductState


@dataclass(frozen=True)
class _Line:
    line_id: int
    product: _Product
    qty: int


@dataclass(frozen=True)
class _Shipment:
    sid: int
    carrier: str
    shipped: datetime
    delivered: datetime


@dataclass(frozen=True)
class _Order:
    oid: int
    customer: _Customer
    placed: datetime
    channel: _Channel
    lines: tuple[_Line, ...]
    # Status changes in time order; the first is always (placed, PENDING).
    events: tuple[tuple[datetime, _Status], ...]
    voided: datetime | None
    shipment: _Shipment | None

    @property
    def total_cents(self) -> int:
        return sum(line.qty * line.product.price_cents for line in self.lines)

    def status_at(self, cutoff: datetime) -> _Status:
        return [status for when, status in self.events if when < cutoff][-1]

    def voided_by(self, cutoff: datetime) -> bool:
        return self.voided is not None and self.voided < cutoff


@dataclass(frozen=True)
class _World:
    regions: tuple[str, ...]
    categories: tuple[str, ...]
    carriers: tuple[str, ...]
    customers: tuple[_Customer, ...]
    products: tuple[_Product, ...]
    orders: tuple[_Order, ...]


def _seconds(rng: random.Random, low: int, high: int) -> timedelta:
    return timedelta(seconds=rng.randint(low, high))


def _delivery_time(rng: random.Random) -> timedelta:
    # Keep durations away from whole days so "more than N days" never hinges on
    # floating-point julianday arithmetic.
    while True:
        seconds = rng.randint(86_400, 9 * 86_400)
        if 120 < seconds % 86_400 < 86_400 - 120:
            return timedelta(seconds=seconds)


def _make_customers(rng: random.Random, regions: Sequence[str]) -> tuple[_Customer, ...]:
    full_names = [f"{first} {last}" for first in _FIRST_NAMES for last in _LAST_NAMES]
    names = rng.sample(full_names, _CUSTOMER_COUNT)
    first_day = date(2023, 1, 1)
    span = (date(2025, 12, 15) - first_day).days
    signups = sorted(first_day + timedelta(days=rng.randrange(span)) for _ in names)
    acct_numbers = rng.sample(range(500_000, 1_000_000), len(names))
    segments = rng.choices(list(_Segment), weights=[72, 20, 8], k=len(names))
    return tuple(
        _Customer(
            cid=1001 + i,
            acct_no=acct_numbers[i],
            name=names[i],
            region=rng.choice(regions),
            signup=signups[i],
            segment=segments[i],
        )
        for i in range(len(names))
    )


def _make_products(rng: random.Random, categories: Sequence[str]) -> tuple[_Product, ...]:
    products: list[_Product] = []
    for category in categories:
        (low, high), nouns = _CATEGORIES[category]
        titles = [f"{adj} {noun}" for adj in _ADJECTIVES for noun in nouns]
        for title in rng.sample(titles, _PRODUCTS_PER_CATEGORY):
            products.append(
                _Product(
                    pid=5001 + len(products),
                    name=title,
                    category=category,
                    price_cents=rng.randint(low, high) * 100 + rng.choice((49, 95, 99)),
                    state=rng.choices(list(_ProductState), weights=[70, 20, 10])[0],
                )
            )
    return tuple(products)


def _timeline(
    rng: random.Random, placed: datetime
) -> tuple[list[tuple[datetime, _Status]], datetime | None, tuple[datetime, datetime] | None]:
    """Return (status events, void time, (shipped, delivered)) for one order."""
    paid = placed + _seconds(rng, 5 * 60, 3 * 3600)
    roll = rng.random()
    if roll < _VOID_RATE:
        voided = placed + _seconds(rng, 3600, 30 * 3600)
        events = [(placed, _Status.PENDING)] + ([(paid, _Status.PAID)] if paid < voided else [])
        return events, voided, None
    if roll < _VOID_RATE + _CANCEL_RATE:
        cancelled = placed + _seconds(rng, 3600, 60 * 3600)
        events = [(placed, _Status.PENDING)]
        events += [(paid, _Status.PAID)] if paid < cancelled else []
        return events + [(cancelled, _Status.CANCELLED)], None, None
    shipped = paid + _seconds(rng, 10 * 3600, 72 * 3600)
    delivered = shipped + _delivery_time(rng)
    events = [
        (placed, _Status.PENDING),
        (paid, _Status.PAID),
        (shipped, _Status.SHIPPED),
        (delivered, _Status.DELIVERED),
    ]
    if rng.random() < _RETURN_RATE:
        events.append((delivered + _seconds(rng, 2 * 86_400, 20 * 86_400), _Status.RETURNED))
    return events, None, (shipped, delivered)


def _make_orders(
    rng: random.Random,
    customers: Sequence[_Customer],
    products: Sequence[_Product],
    carriers: Sequence[str],
) -> tuple[_Order, ...]:
    signups = [c.signup for c in customers]
    activity = list(accumulate(rng.lognormvariate(0, 0.8) for _ in customers))
    product_weights = list(
        accumulate(
            {_ProductState.ACTIVE: 1.0, _ProductState.DISCONTINUED: 0.5}.get(p.state, 0.3)
            for p in products
        )
    )
    placements: list[tuple[datetime, _Customer]] = []
    day = date(2025, 1, 1)
    while day < _EPOCH_CUTOFFS[max(_EPOCH_CUTOFFS)].date():
        eligible = bisect_left(signups, day)
        for _ in range(rng.randint(*_ORDERS_PER_DAY)):
            placed = datetime.combine(day, time()) + _seconds(rng, 0, 86_399)
            customer = rng.choices(customers[:eligible], cum_weights=activity[:eligible])[0]
            placements.append((placed, customer))
        day += timedelta(days=1)
    placements.sort(key=lambda p: p[0])

    orders: list[_Order] = []
    next_line = 900_001
    next_shipment = 70_001
    for index, (placed, customer) in enumerate(placements):
        lines: list[_Line] = []
        for _ in range(rng.choices((1, 2, 3, 4), weights=(45, 30, 17, 8))[0]):
            product = rng.choices(products, cum_weights=product_weights)[0]
            qty = rng.choices((1, 2, 3), weights=(70, 22, 8))[0]
            lines.append(_Line(line_id=next_line, product=product, qty=qty))
            next_line += 1
        events, voided, shipping = _timeline(rng, placed)
        shipment = None
        if shipping is not None:
            carrier = rng.choices(carriers, weights=_CARRIER_WEIGHTS)[0]
            shipment = _Shipment(next_shipment, carrier, shipping[0], shipping[1])
            next_shipment += 1
        orders.append(
            _Order(
                oid=100_001 + index,
                customer=customer,
                placed=placed,
                channel=rng.choices(list(_Channel), weights=(45, 30, 18, 7))[0],
                lines=tuple(lines),
                events=tuple(events),
                voided=voided,
                shipment=shipment,
            )
        )
    return tuple(orders)


def _make_world(rng: random.Random) -> _World:
    categories = tuple(rng.sample(list(_CATEGORIES), _CATEGORY_COUNT))
    carriers = tuple(rng.sample(_CARRIERS, len(_CARRIER_WEIGHTS)))
    customers = _make_customers(rng, _REGIONS)
    products = _make_products(rng, categories)
    return _World(
        regions=_REGIONS,
        categories=categories,
        carriers=carriers,
        customers=customers,
        products=products,
        orders=_make_orders(rng, customers, products, carriers),
    )


# ---------------------------------------------------------------------------
# Truth as of a cutoff (what every expected answer is computed from)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _OrderState:
    order: _Order
    status: _Status


@dataclass(frozen=True)
class _ShipState:
    sid: int
    oid: int
    carrier: str
    shipped: datetime
    delivered: datetime | None


@dataclass(frozen=True)
class _View:
    customers: tuple[_Customer, ...]
    products: tuple[_Product, ...]
    # Orders that count for business metrics: placed and not voided by the cutoff.
    orders: tuple[_OrderState, ...]
    shipments: tuple[_ShipState, ...]


def _visible_customers(world: _World, cutoff: datetime) -> tuple[_Customer, ...]:
    return tuple(c for c in world.customers if c.signup < cutoff.date())


def _ship_states(world: _World, cutoff: datetime) -> tuple[_ShipState, ...]:
    return tuple(
        _ShipState(
            sid=s.sid,
            oid=o.oid,
            carrier=s.carrier,
            shipped=s.shipped,
            delivered=s.delivered if s.delivered < cutoff else None,
        )
        for o in world.orders
        if (s := o.shipment) is not None and s.shipped < cutoff
    )


def _view(world: _World, cutoff: datetime) -> _View:
    return _View(
        customers=_visible_customers(world, cutoff),
        products=world.products,
        orders=tuple(
            _OrderState(o, o.status_at(cutoff))
            for o in world.orders
            if o.placed < cutoff and not o.voided_by(cutoff)
        ),
        shipments=_ship_states(world, cutoff),
    )


# ---------------------------------------------------------------------------
# Design: physical names, codes, units, formats and migrations
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Migrations:
    # Fact kind -> first epoch that follows the new rule.
    change_epoch: Mapping[str, int]
    renamed_column: Literal["customer_region", "order_placed"]

    def active(self, kind: str, epoch: int) -> bool:
        first = self.change_epoch.get(kind)
        return first is not None and epoch >= first


@dataclass(frozen=True)
class _Design:
    names: Mapping[str, str]
    migrations: _Migrations
    initial_amount_unit: Unit
    price_unit: Unit
    order_time: TimeFormat
    ship_time: TimeFormat
    status_codes: Mapping[_Status, int]
    reassigned_status_codes: Mapping[_Status, int]
    channel_codes: Mapping[_Channel, str]
    segment_codes: Mapping[_Segment, str]
    state_codes: Mapping[_ProductState, str]
    # Leaf fact keys targeted by the false notes after sessions 3 and 7.
    injection_leaves: tuple[str, str]
    # The member a false note claims the leaf code stands for.
    decoy_state: _ProductState
    decoy_segment: _Segment


def _leaf_codes[M](rng: random.Random, members: Sequence[M]) -> dict[M, str]:
    letters = rng.sample("BCDFGHJKLMNPQRSTVWXZ", len(members))
    return {m: f"{letter}{rng.randint(1, 9)}" for m, letter in zip(members, letters, strict=True)}


def _derangement(rng: random.Random, codes: Mapping[_Status, int]) -> dict[_Status, int]:
    """Reassign every status to another status's old code, so old filters hit wrong rows."""
    values = list(codes.values())
    while True:
        shuffled = rng.sample(values, len(values))
        if all(a != b for a, b in zip(values, shuffled, strict=True)):
            return dict(zip(codes, shuffled, strict=True))


def _make_design(rng: random.Random) -> _Design:
    names = {slot: rng.choice(options) for slot, options in sorted(_NAME_CHOICES.items())}
    visible = rng.sample(["column_name", "join_key", "stale_table"], 2)
    semantic = rng.sample(["unit", "status_code", "soft_delete"], 2)
    migrations = _Migrations(
        change_epoch={visible[0]: 2, semantic[0]: 2, visible[1]: 3, semantic[1]: 3},
        renamed_column=rng.choice(("customer_region", "order_placed")),
    )
    order_time: TimeFormat = rng.choice(("unix", "iso", "yyyymmdd"))
    ship_time: TimeFormat = rng.choice([f for f in ("unix", "iso") if f != order_time])
    status_codes = dict(zip(_Status, rng.sample(range(1, 10), len(_Status)), strict=True))
    injection_leaves = rng.sample([_LEAF_PRODUCT, _LEAF_CUSTOMER], 2)
    return _Design(
        names=names,
        migrations=migrations,
        initial_amount_unit=rng.choice(("cents", "dollars")),
        price_unit=rng.choice(("cents", "dollars")),
        order_time=order_time,
        ship_time=ship_time,
        status_codes=status_codes,
        reassigned_status_codes=_derangement(rng, status_codes),
        channel_codes=_leaf_codes(rng, list(_Channel)),
        segment_codes=_leaf_codes(rng, list(_Segment)),
        state_codes=_leaf_codes(rng, list(_ProductState)),
        injection_leaves=(injection_leaves[0], injection_leaves[1]),
        decoy_state=rng.choice((_ProductState.ACTIVE, _ProductState.PREORDER)),
        decoy_segment=rng.choice((_Segment.CONSUMER, _Segment.EMPLOYEE)),
    )


@dataclass(frozen=True)
class _ShipTable:
    name: str
    pk: str
    order: str
    carrier: str
    shipped: str
    delivered: str
    # Rows reflect the shipments as of this epoch's cutoff.
    as_of_epoch: int


@dataclass(frozen=True)
class _Schema:
    epoch: int
    customers: str
    c_pk: str
    c_name: str
    c_region: str
    c_signup: str
    c_segment: str
    c_acct: str | None
    products: str
    p_pk: str
    p_name: str
    p_category: str
    p_price: str
    p_state: str
    orders: str
    o_pk: str
    o_cust: str
    o_placed: str
    o_status: str
    o_channel: str
    o_amount: str
    o_void: str | None
    lines: str
    l_pk: str
    l_order: str
    l_product: str
    l_qty: str
    l_price: str
    ref: str
    r_domain: str
    r_code: str
    r_label: str
    shipments: _ShipTable
    stale_shipments: _ShipTable | None
    amount_unit: Unit
    price_unit: Unit
    order_time: TimeFormat
    ship_time: TimeFormat
    status_codes: Mapping[_Status, int]
    channel_codes: Mapping[_Channel, str]
    segment_codes: Mapping[_Segment, str]
    state_codes: Mapping[_ProductState, str]

    @property
    def customer_key(self) -> str:
        """The customers column that orders reference."""
        return self.c_acct or self.c_pk


def _schema(d: _Design, epoch: int) -> _Schema:
    n, m = d.names, d.migrations
    renamed = m.active("column_name", epoch)
    rekeyed = m.active("join_key", epoch)
    old_ship = _ShipTable(
        n["ship"], n["s_pk"], n["s_order"], n["s_carrier"], n["s_shipped"], n["s_delivered"], epoch
    )
    shipments, stale = old_ship, None
    if m.active("stale_table", epoch):
        shipments = _ShipTable(
            n["ship_v2"],
            n["s2_pk"],
            n["s2_order"],
            n["s2_carrier"],
            n["s2_shipped"],
            n["s2_delivered"],
            epoch,
        )
        # The old table stopped receiving updates at the migration.
        stale = replace(old_ship, as_of_epoch=m.change_epoch["stale_table"] - 1)
    unit_flipped = m.active("unit", epoch)
    amount_unit: Unit = d.initial_amount_unit
    if unit_flipped:
        amount_unit = "dollars" if d.initial_amount_unit == "cents" else "cents"
    return _Schema(
        epoch=epoch,
        customers=n["customers"],
        c_pk=n["c_pk"],
        c_name=n["c_name"],
        c_region=n["c_region_v2"]
        if renamed and m.renamed_column == "customer_region"
        else n["c_region"],
        c_signup=n["c_signup"],
        c_segment=n["c_segment"],
        c_acct=n["c_acct"] if rekeyed else None,
        products=n["products"],
        p_pk=n["p_pk"],
        p_name=n["p_name"],
        p_category=n["p_category"],
        p_price=n["p_price"],
        p_state=n["p_state"],
        orders=n["orders"],
        o_pk=n["o_pk"],
        o_cust=n["o_cust_v2"] if rekeyed else n["o_cust"],
        o_placed=n["o_placed_v2"]
        if renamed and m.renamed_column == "order_placed"
        else n["o_placed"],
        o_status=n["o_status"],
        o_channel=n["o_channel"],
        o_amount=n["o_amount"],
        o_void=n["o_void"] if m.active("soft_delete", epoch) else None,
        lines=n["lines"],
        l_pk=n["l_pk"],
        l_order=n["l_order"],
        l_product=n["l_product"],
        l_qty=n["l_qty"],
        l_price=n["l_price"],
        ref=n["ref"],
        r_domain=n["r_domain"],
        r_code=n["r_code"],
        r_label=n["r_label"],
        shipments=shipments,
        stale_shipments=stale,
        amount_unit=amount_unit,
        price_unit=d.price_unit,
        order_time=d.order_time,
        ship_time=d.ship_time,
        status_codes=d.reassigned_status_codes
        if m.active("status_code", epoch)
        else d.status_codes,
        channel_codes=d.channel_codes,
        segment_codes=d.segment_codes,
        state_codes=d.state_codes,
    )


# ---------------------------------------------------------------------------
# Rendering: value encodings and SQL literals share one definition per format
# ---------------------------------------------------------------------------


def _money(cents: int, unit: Unit) -> int | float:
    return cents if unit == "cents" else round(cents / 100, 2)


def _money_literal(dollars: int, unit: Unit) -> str:
    return str(dollars * 100 if unit == "cents" else dollars)


def _encode_time(moment: datetime, fmt: TimeFormat) -> int | str:
    if fmt == "unix":
        return calendar.timegm(moment.timetuple())
    if fmt == "iso":
        return moment.strftime("%Y-%m-%d %H:%M:%S")
    return int(moment.strftime("%Y%m%d"))


def _time_literal(day: date, fmt: TimeFormat) -> str:
    encoded = _encode_time(datetime.combine(day, time()), fmt)
    return _sql_text(encoded) if isinstance(encoded, str) else str(encoded)


def _time_type(fmt: TimeFormat) -> str:
    return "TEXT" if fmt == "iso" else "INTEGER"


def _sql_text(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


@dataclass(frozen=True)
class _Table:
    name: str
    columns: list[tuple[str, str]]
    rows: list[tuple[Any, ...]]


def _ref_table(s: _Schema) -> _Table:
    rows = [("ORDER_STATUS", str(code), status.value) for status, code in s.status_codes.items()]
    rows += [("SALES_CHANNEL", code, ch.value) for ch, code in s.channel_codes.items()]
    rows += [("CUSTOMER_SEGMENT", code, seg.value) for seg, code in s.segment_codes.items()]
    rows += [("PRODUCT_STATE", code, st.value) for st, code in s.state_codes.items()]
    if s.o_void is not None:
        rows += [
            ("ORDER_VOID_FLAG", "0", "Live order"),
            ("ORDER_VOID_FLAG", "1", "Voided order - exclude from all reporting"),
        ]
    columns = [(s.r_domain, "TEXT"), (s.r_code, "TEXT"), (s.r_label, "TEXT")]
    return _Table(s.ref, columns, sorted(rows))


def _customer_table(world: _World, s: _Schema, cutoff: datetime) -> _Table:
    columns = [
        (s.c_pk, "INTEGER PRIMARY KEY"),
        (s.c_name, "TEXT"),
        (s.c_region, "TEXT"),
        (s.c_signup, "TEXT"),
        (s.c_segment, "TEXT"),
    ] + ([(s.c_acct, "INTEGER")] if s.c_acct else [])
    rows = [
        (c.cid, c.name, c.region, c.signup.isoformat(), s.segment_codes[c.segment])
        + ((c.acct_no,) if s.c_acct else ())
        for c in _visible_customers(world, cutoff)
    ]
    return _Table(s.customers, columns, rows)


def _product_table(world: _World, s: _Schema) -> _Table:
    columns = [
        (s.p_pk, "INTEGER PRIMARY KEY"),
        (s.p_name, "TEXT"),
        (s.p_category, "TEXT"),
        (s.p_price, "NUMERIC"),
        (s.p_state, "TEXT"),
    ]
    rows = [
        (p.pid, p.name, p.category, _money(p.price_cents, s.price_unit), s.state_codes[p.state])
        for p in world.products
    ]
    return _Table(s.products, columns, rows)


def _order_tables(world: _World, s: _Schema, cutoff: datetime) -> tuple[_Table, _Table]:
    """Orders and their lines. Before soft deletes exist, voided orders are deleted outright."""
    present = [
        o
        for o in world.orders
        if o.placed < cutoff and (s.o_void is not None or not o.voided_by(cutoff))
    ]
    order_columns = [
        (s.o_pk, "INTEGER PRIMARY KEY"),
        (s.o_cust, "INTEGER"),
        (s.o_placed, _time_type(s.order_time)),
        (s.o_status, "INTEGER"),
        (s.o_channel, "TEXT"),
        (s.o_amount, "NUMERIC"),
    ] + ([(s.o_void, "INTEGER")] if s.o_void else [])
    order_rows = [
        (
            o.oid,
            o.customer.acct_no if s.c_acct else o.customer.cid,
            _encode_time(o.placed, s.order_time),
            s.status_codes[o.status_at(cutoff)],
            s.channel_codes[o.channel],
            _money(o.total_cents, s.amount_unit),
        )
        + ((int(o.voided_by(cutoff)),) if s.o_void else ())
        for o in present
    ]
    line_columns = [
        (s.l_pk, "INTEGER PRIMARY KEY"),
        (s.l_order, "INTEGER"),
        (s.l_product, "INTEGER"),
        (s.l_qty, "INTEGER"),
        (s.l_price, "NUMERIC"),
    ]
    line_rows = [
        (
            line.line_id,
            o.oid,
            line.product.pid,
            line.qty,
            _money(line.product.price_cents, s.price_unit),
        )
        for o in present
        for line in o.lines
    ]
    return _Table(s.orders, order_columns, order_rows), _Table(s.lines, line_columns, line_rows)


def _ship_table(world: _World, table: _ShipTable, fmt: TimeFormat) -> _Table:
    columns = [
        (table.pk, "INTEGER PRIMARY KEY"),
        (table.order, "INTEGER"),
        (table.carrier, "TEXT"),
        (table.shipped, _time_type(fmt)),
        (table.delivered, _time_type(fmt)),
    ]
    rows = [
        (
            ship.sid,
            ship.oid,
            ship.carrier,
            _encode_time(ship.shipped, fmt),
            None if ship.delivered is None else _encode_time(ship.delivered, fmt),
        )
        for ship in _ship_states(world, _EPOCH_CUTOFFS[table.as_of_epoch])
    ]
    return _Table(table.name, columns, rows)


def _write_db(path: Path, world: _World, s: _Schema) -> None:
    cutoff = _EPOCH_CUTOFFS[s.epoch]
    tables = [
        _ref_table(s),
        _customer_table(world, s, cutoff),
        _product_table(world, s),
        *_order_tables(world, s, cutoff),
        _ship_table(world, s.shipments, s.ship_time),
    ]
    if s.stale_shipments is not None:
        tables.append(_ship_table(world, s.stale_shipments, s.ship_time))
    path.unlink(missing_ok=True)
    conn = sqlite3.connect(path)
    try:
        for table in tables:
            columns = ", ".join(f"{name} {kind}" for name, kind in table.columns)
            conn.execute(f"CREATE TABLE {table.name} ({columns})")
            placeholders = ", ".join("?" for _ in table.columns)
            conn.executemany(f"INSERT INTO {table.name} VALUES ({placeholders})", table.rows)
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Facts: derived per epoch from the schema; a change in value starts a new fact
# ---------------------------------------------------------------------------

_FactSpec = tuple[FactKind, str, str, bool]


def _fact_specs(s: _Schema) -> dict[str, _FactSpec]:
    specs: dict[str, _FactSpec] = {
        "join.orders_customers": (
            "join_key",
            "join condition between orders and customers",
            f"{s.orders}.{s.o_cust} = {s.customers}.{s.customer_key}",
            False,
        ),
        "join.lines_orders": (
            "join_key",
            "join condition between order lines and orders",
            f"{s.lines}.{s.l_order} = {s.orders}.{s.o_pk}",
            False,
        ),
        "unit.order_amount": (
            "unit",
            f"unit of the order total amount ({s.orders}.{s.o_amount})",
            s.amount_unit,
            False,
        ),
        "unit.prices": (
            "unit",
            f"unit of product list prices and order line unit prices "
            f"({s.products}.{s.p_price}, {s.lines}.{s.l_price})",
            s.price_unit,
            False,
        ),
        "time.order_placed": (
            "time_format",
            "format of order placement times",
            TIME_FORMAT_LABELS[s.order_time],
            False,
        ),
        "time.shipment": (
            "time_format",
            "format of shipment dispatch and delivery times",
            TIME_FORMAT_LABELS[s.ship_time],
            False,
        ),
        "column.customer_region": (
            "column_name",
            f"column of {s.customers} that holds the customer's sales region",
            f"{s.customers}.{s.c_region}",
            False,
        ),
        "column.order_placed": (
            "column_name",
            f"column of {s.orders} that holds the order placement time",
            f"{s.orders}.{s.o_placed}",
            False,
        ),
        "table.shipments": (
            "stale_table",
            "table with the up-to-date shipment and delivery records",
            s.shipments.name,
            False,
        ),
        "leaf.channel_marketplace": (
            "status_code",
            f"sales channel code ({s.orders}.{s.o_channel}) for partner marketplace orders",
            s.channel_codes[_Channel.MARKETPLACE],
            True,
        ),
        _LEAF_PRODUCT: (
            "status_code",
            f"product lifecycle code ({s.products}.{s.p_state}) for discontinued products",
            s.state_codes[_ProductState.DISCONTINUED],
            True,
        ),
        _LEAF_CUSTOMER: (
            "status_code",
            f"customer segment code ({s.customers}.{s.c_segment}) for business accounts",
            s.segment_codes[_Segment.BUSINESS],
            True,
        ),
    }
    for status in _FACT_STATUSES:
        specs[f"status.{status.name.lower()}"] = (
            "status_code",
            f"order status code ({s.orders}.{s.o_status}) for {status.value.lower()} orders",
            str(s.status_codes[status]),
            False,
        )
    if s.o_void is not None:
        specs["softdelete.orders"] = (
            "soft_delete",
            "condition that marks voided orders, which must be excluded from all order metrics",
            f"{s.orders}.{s.o_void} = 1",
            False,
        )
    return specs


@dataclass(frozen=True)
class _FactIndex:
    by_key: Mapping[str, tuple[Fact, ...]]

    @classmethod
    def build(cls, schemas: Mapping[int, _Schema]) -> "_FactIndex":
        history: dict[str, list[Fact]] = defaultdict(list)
        for epoch in sorted(schemas):
            start = epoch_start(epoch)
            # Keys never disappear in a later epoch (soft deletes only ever appear).
            for key, (kind, subject, value, leaf) in _fact_specs(schemas[epoch]).items():
                versions = history[key]
                current = versions[-1] if versions else None
                if current is not None and current.valid_until_session is None:
                    if (current.subject, current.value) == (subject, value):
                        continue
                    versions[-1] = current.model_copy(update={"valid_until_session": start - 1})
                versions.append(
                    Fact(
                        fact_id=f"{key}@s{start}",
                        kind=kind,
                        subject=subject,
                        value=value,
                        valid_from_session=start,
                        valid_until_session=None,
                        leaf=leaf,
                    )
                )
        return cls({key: tuple(versions) for key, versions in history.items()})

    def all(self) -> list[Fact]:
        return sorted((f for fs in self.by_key.values() for f in fs), key=lambda f: f.fact_id)

    def at(self, key: str, session: int) -> Fact:
        fact = next((f for f in self.by_key.get(key, ()) if f.valid_at(session)), None)
        if fact is None:
            raise KeyError(f"no fact {key} is valid in session {session}")
        return fact

    def changes_at(self, key: str, session: int) -> bool:
        return any(f.valid_from_session == session for f in self.by_key.get(key, ()))


# ---------------------------------------------------------------------------
# Queries: one semantic spec, two independent implementations (SQL and Python)
# ---------------------------------------------------------------------------


class _Query(ABC):
    @property
    def ordered(self) -> bool:
        return False

    def tie_free(self, view: _View) -> bool:
        return True

    @abstractmethod
    def keys(self, s: _Schema) -> list[str]:
        """Fact keys whose rules this question depends on under schema `s`."""

    @abstractmethod
    def sql(self, s: _Schema) -> str: ...

    @abstractmethod
    def answer(self, view: _View) -> Rows: ...


def _where(conditions: Sequence[str]) -> str:
    return f" WHERE {' AND '.join(conditions)}" if conditions else ""


@dataclass(frozen=True)
class _OrderQuery(_Query):
    measure: Literal["count", "value", "avg_value", "units"]
    status: _Status | None = None
    region: str | None = None
    marketplace: bool = False
    placed_from: date | None = None
    placed_before: date | None = None
    min_dollars: int | None = None
    category: str | None = None
    group: Literal["region", "customer", "category"] | None = None
    top_k: int | None = None

    def __post_init__(self) -> None:
        if (self.category is not None or self.group == "category") and self.measure != "units":
            raise ValueError("product categories are only reachable through order lines")
        if self.top_k is not None and self.group not in ("customer", "category"):
            raise ValueError("top_k ranks customers or categories")

    @property
    def ordered(self) -> bool:
        return self.top_k is not None

    @property
    def _joins_customers(self) -> bool:
        return self.region is not None or self.group in ("region", "customer")

    def keys(self, s: _Schema) -> list[str]:
        keys: list[str] = []
        if self.status is not None:
            keys.append(f"status.{self.status.name.lower()}")
        elif s.o_void is not None:
            # Voided orders keep a live-looking status, so only unfiltered counts see them.
            keys.append("softdelete.orders")
        if self._joins_customers:
            keys.append("join.orders_customers")
        if self.region is not None or self.group == "region":
            keys.append("column.customer_region")
        if self.marketplace:
            keys.append("leaf.channel_marketplace")
        if self.placed_from is not None or self.placed_before is not None:
            keys += ["time.order_placed", "column.order_placed"]
        if self.measure in ("value", "avg_value") or self.min_dollars is not None:
            keys.append("unit.order_amount")
        if self.measure == "units":
            keys.append("join.lines_orders")
        return keys

    def sql(self, s: _Schema) -> str:
        joins = []
        if self._joins_customers:
            joins.append(f"JOIN {s.customers} c ON o.{s.o_cust} = c.{s.customer_key}")
        if self.measure == "units":
            joins.append(f"JOIN {s.lines} l ON l.{s.l_order} = o.{s.o_pk}")
            joins.append(f"JOIN {s.products} p ON p.{s.p_pk} = l.{s.l_product}")
        conditions = [f"o.{s.o_void} = 0"] if s.o_void is not None else []
        if self.status is not None:
            conditions.append(f"o.{s.o_status} = {s.status_codes[self.status]}")
        if self.region is not None:
            conditions.append(f"c.{s.c_region} = {_sql_text(self.region)}")
        if self.marketplace:
            marketplace = s.channel_codes[_Channel.MARKETPLACE]
            conditions.append(f"o.{s.o_channel} = {_sql_text(marketplace)}")
        if self.placed_from is not None:
            conditions.append(f"o.{s.o_placed} >= {_time_literal(self.placed_from, s.order_time)}")
        if self.placed_before is not None:
            conditions.append(f"o.{s.o_placed} < {_time_literal(self.placed_before, s.order_time)}")
        if self.min_dollars is not None:
            conditions.append(f"o.{s.o_amount} > {_money_literal(self.min_dollars, s.amount_unit)}")
        if self.category is not None:
            conditions.append(f"p.{s.p_category} = {_sql_text(self.category)}")
        to_dollars = " / 100.0" if s.amount_unit == "cents" else ""
        measure = {
            "count": "COUNT(*)",
            "value": f"SUM(o.{s.o_amount}){to_dollars}",
            "avg_value": f"AVG(o.{s.o_amount}){to_dollars}",
            "units": f"SUM(l.{s.l_qty})",
        }[self.measure]
        source = " ".join([f"FROM {s.orders} o", *joins]) + _where(conditions)
        if self.group is None:
            return f"SELECT {measure} {source}"
        label, key = {
            "region": (f"c.{s.c_region}", f"c.{s.c_region}"),
            "customer": (f"c.{s.c_name}", f"c.{s.customer_key}"),
            "category": (f"p.{s.p_category}", f"p.{s.p_category}"),
        }[self.group]
        order = f" ORDER BY measure DESC LIMIT {self.top_k}" if self.top_k is not None else ""
        return f"SELECT {label}, {measure} AS measure {source} GROUP BY {key}{order}"

    def _matches(self, state: _OrderState) -> bool:
        o = state.order
        return (
            (self.status is None or state.status == self.status)
            and (self.region is None or o.customer.region == self.region)
            and (not self.marketplace or o.channel == _Channel.MARKETPLACE)
            and (self.placed_from is None or o.placed.date() >= self.placed_from)
            and (self.placed_before is None or o.placed.date() < self.placed_before)
            and (self.min_dollars is None or o.total_cents > self.min_dollars * 100)
        )

    def _label(self, state: _OrderState, line: _Line | None) -> str:
        if self.group == "region":
            return state.order.customer.region
        if self.group == "customer":
            return state.order.customer.name
        if self.group == "category" and line is not None:
            return line.product.category
        return ""

    def _contributions(self, view: _View) -> list[tuple[str, int]]:
        """(group label, amount) pairs: order totals in cents, or line quantities for units."""
        out: list[tuple[str, int]] = []
        for state in view.orders:
            if not self._matches(state):
                continue
            if self.measure != "units":
                out.append((self._label(state, None), state.order.total_cents))
                continue
            for line in state.order.lines:
                if self.category is None or line.product.category == self.category:
                    out.append((self._label(state, line), line.qty))
        return out

    def _aggregate(self, amounts: Sequence[int]) -> int | float | None:
        if self.measure == "count":
            return len(amounts)
        if not amounts:
            return None
        if self.measure == "value":
            return sum(amounts) / 100
        if self.measure == "avg_value":
            return sum(amounts) / len(amounts) / 100
        return sum(amounts)

    def _groups(self, view: _View) -> list[list[Any]]:
        grouped: dict[str, list[int]] = defaultdict(list)
        for label, amount in self._contributions(view):
            grouped[label].append(amount)
        return [[label, self._aggregate(amounts)] for label, amounts in grouped.items()]

    def answer(self, view: _View) -> Rows:
        if self.group is None:
            return [[self._aggregate([amount for _, amount in self._contributions(view)])]]
        rows = self._groups(view)
        if self.top_k is None:
            return sorted(rows)
        rows.sort(key=lambda row: row[1], reverse=True)
        return rows[: self.top_k]

    def tie_free(self, view: _View) -> bool:
        if self.top_k is None:
            return True
        values = sorted((row[1] for row in self._groups(view)), reverse=True)[: self.top_k + 1]
        return len(values) >= self.top_k and len(set(values)) == len(values)


@dataclass(frozen=True)
class _ShipmentQuery(_Query):
    state: Literal["delivered", "in_transit", "dispatched"]
    carrier: str | None = None
    slower_than_days: int | None = None
    shipped_from: date | None = None
    by_carrier: bool = False

    def keys(self, s: _Schema) -> list[str]:
        timed = self.slower_than_days is not None or self.shipped_from is not None
        return ["table.shipments"] + (["time.shipment"] if timed else [])

    def sql(self, s: _Schema) -> str:
        t = s.shipments
        conditions = []
        if self.state == "delivered":
            conditions.append(f"s.{t.delivered} IS NOT NULL")
        elif self.state == "in_transit":
            conditions.append(f"s.{t.delivered} IS NULL")
        if self.carrier is not None:
            conditions.append(f"s.{t.carrier} = {_sql_text(self.carrier)}")
        if self.slower_than_days is not None:
            if s.ship_time == "unix":
                elapsed = f"s.{t.delivered} - s.{t.shipped} > {self.slower_than_days * 86_400}"
            else:
                elapsed = f"julianday(s.{t.delivered}) - julianday(s.{t.shipped}) > {self.slower_than_days}"
            conditions.append(elapsed)
        if self.shipped_from is not None:
            conditions.append(f"s.{t.shipped} >= {_time_literal(self.shipped_from, s.ship_time)}")
        source = f"FROM {t.name} s" + _where(conditions)
        if self.by_carrier:
            return f"SELECT s.{t.carrier}, COUNT(*) {source} GROUP BY s.{t.carrier}"
        return f"SELECT COUNT(*) {source}"

    def _matches(self, ship: _ShipState) -> bool:
        if self.state == "delivered" and ship.delivered is None:
            return False
        if self.state == "in_transit" and ship.delivered is not None:
            return False
        if self.slower_than_days is not None and (
            ship.delivered is None
            or ship.delivered - ship.shipped <= timedelta(days=self.slower_than_days)
        ):
            return False
        return (self.carrier is None or ship.carrier == self.carrier) and (
            self.shipped_from is None or ship.shipped.date() >= self.shipped_from
        )

    def answer(self, view: _View) -> Rows:
        matching = [ship for ship in view.shipments if self._matches(ship)]
        if not self.by_carrier:
            return [[len(matching)]]
        counts: dict[str, int] = defaultdict(int)
        for ship in matching:
            counts[ship.carrier] += 1
        return sorted([carrier, count] for carrier, count in counts.items())


@dataclass(frozen=True)
class _ProductQuery(_Query):
    state: _ProductState
    category: str | None = None
    min_dollars: int | None = None
    list_names: bool = False

    def keys(self, s: _Schema) -> list[str]:
        return [_LEAF_PRODUCT] + (["unit.prices"] if self.min_dollars is not None else [])

    def sql(self, s: _Schema) -> str:
        conditions = [f"p.{s.p_state} = {_sql_text(s.state_codes[self.state])}"]
        if self.category is not None:
            conditions.append(f"p.{s.p_category} = {_sql_text(self.category)}")
        if self.min_dollars is not None:
            conditions.append(f"p.{s.p_price} > {_money_literal(self.min_dollars, s.price_unit)}")
        select = f"p.{s.p_name}" if self.list_names else "COUNT(*)"
        return f"SELECT {select} FROM {s.products} p" + _where(conditions)

    def answer(self, view: _View) -> Rows:
        matching = [
            p
            for p in view.products
            if p.state == self.state
            and (self.category is None or p.category == self.category)
            and (self.min_dollars is None or p.price_cents > self.min_dollars * 100)
        ]
        if self.list_names:
            return sorted([p.name] for p in matching)
        return [[len(matching)]]


@dataclass(frozen=True)
class _CustomerQuery(_Query):
    segment: _Segment | None = None
    region: str | None = None
    signed_from: date | None = None
    signed_before: date | None = None
    list_names: bool = False
    without_orders: bool = False

    def keys(self, s: _Schema) -> list[str]:
        keys = [_LEAF_CUSTOMER] if self.segment is not None else []
        if self.region is not None:
            keys.append("column.customer_region")
        if self.without_orders:
            keys.append("join.orders_customers")
            if s.o_void is not None:
                keys.append("softdelete.orders")
        return keys

    def sql(self, s: _Schema) -> str:
        conditions = []
        if self.segment is not None:
            conditions.append(f"c.{s.c_segment} = {_sql_text(s.segment_codes[self.segment])}")
        if self.region is not None:
            conditions.append(f"c.{s.c_region} = {_sql_text(self.region)}")
        if self.signed_from is not None:
            conditions.append(f"c.{s.c_signup} >= {_sql_text(self.signed_from.isoformat())}")
        if self.signed_before is not None:
            conditions.append(f"c.{s.c_signup} < {_sql_text(self.signed_before.isoformat())}")
        if self.without_orders:
            live = f" AND o.{s.o_void} = 0" if s.o_void is not None else ""
            conditions.append(
                f"NOT EXISTS (SELECT 1 FROM {s.orders} o "
                f"WHERE o.{s.o_cust} = c.{s.customer_key}{live})"
            )
        select = f"c.{s.c_name}" if self.list_names else "COUNT(*)"
        return f"SELECT {select} FROM {s.customers} c" + _where(conditions)

    def answer(self, view: _View) -> Rows:
        buyers = {state.order.customer.cid for state in view.orders}
        matching = [
            c
            for c in view.customers
            if (self.segment is None or c.segment == self.segment)
            and (self.region is None or c.region == self.region)
            and (self.signed_from is None or c.signup >= self.signed_from)
            and (self.signed_before is None or c.signup < self.signed_before)
            and (not self.without_orders or c.cid not in buyers)
        ]
        if self.list_names:
            return sorted([c.name] for c in matching)
        return [[len(matching)]]


# ---------------------------------------------------------------------------
# Question templates: business-language text, never a column name
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Variant:
    template: str
    text: str
    query: _Query
    # For leaf questions: the fact key, and what a believer of the false note computes.
    leaf: str | None = None
    decoy: _Query | None = None


_SINGLE = "Return a single number."


def _month(year: int, month: int) -> date:
    return date(year, month, 1)


def _month_label(day: date) -> str:
    return f"{_MONTH_NAMES[day.month - 1]} {day.year}"


def _next_month(day: date) -> date:
    return date(day.year + day.month // 12, day.month % 12 + 1, 1)


# Months inside epoch 1, so every epoch's answer is non-empty.
_EARLY_MONTHS = tuple(_month(2025, m) for m in (1, 2, 3, 4))
_SINCE_MONTHS = tuple(_month(2025, m) for m in (2, 3, 4))


def _status_variants(world: _World) -> list[_Variant]:
    out: list[_Variant] = []
    for st in _FACT_STATUSES:
        out += [
            _Variant(
                "status_total",
                f"How many orders currently have the status {st.value}? {_SINGLE}",
                _OrderQuery("count", status=st),
            ),
            _Variant(
                "status_value",
                f"What is the total value in US dollars of all orders whose current status "
                f"is {st.value}? {_SINGLE}",
                _OrderQuery("value", status=st),
            ),
        ]
        out += [
            _Variant(
                "status_region",
                f"How many orders placed by customers in the {region} region currently have "
                f"the status {st.value}? {_SINGLE}",
                _OrderQuery("count", status=st, region=region),
            )
            for region in world.regions
        ]
    for st in (_Status.CANCELLED, _Status.RETURNED):
        out.append(
            _Variant(
                "marketplace_status",
                f"How many partner marketplace orders currently have the status {st.value}? "
                f"{_SINGLE}",
                _OrderQuery("count", status=st, marketplace=True),
            )
        )
    return out


def _region_variants(world: _World) -> list[_Variant]:
    delivered = _Status.DELIVERED
    out: list[_Variant] = []
    for region in world.regions:
        out += [
            _Variant(
                "region_orders",
                f"How many orders have been placed by customers in the {region} region to date? "
                f"{_SINGLE}",
                _OrderQuery("count", region=region),
            ),
            _Variant(
                "region_avg_value",
                f"What is the average value in US dollars of orders placed by customers in the "
                f"{region} region whose current status is Delivered? Return the unrounded number.",
                _OrderQuery("avg_value", status=delivered, region=region),
            ),
            _Variant(
                "region_value",
                f"What is the total value in US dollars of orders placed by customers in the "
                f"{region} region whose current status is Delivered? {_SINGLE}",
                _OrderQuery("value", status=delivered, region=region),
            ),
            _Variant(
                "region_without_orders",
                f"How many registered customers in the {region} region have not placed any "
                f"order to date? Cancelled orders count as placed orders. {_SINGLE}",
                _CustomerQuery(region=region, without_orders=True),
            ),
        ]
        out += [
            _Variant(
                "region_since",
                f"How many orders have customers in the {region} region placed since the start "
                f"of {_month_label(month)}? {_SINGLE}",
                _OrderQuery("count", region=region, placed_from=month),
            )
            for month in _SINCE_MONTHS
        ]
    out.append(
        _Variant(
            "region_value_breakdown",
            "For each customer region, what is the total value in US dollars of orders whose "
            "current status is Delivered? Return one row per region with the region name and "
            "the total.",
            _OrderQuery("value", status=delivered, group="region"),
        )
    )
    out.append(
        _Variant(
            "region_count_breakdown",
            "For each customer region, how many orders have been placed to date? Return one row "
            "per region with the region name and the count.",
            _OrderQuery("count", group="region"),
        )
    )
    out += [
        _Variant(
            "region_count_breakdown",
            f"For each customer region, how many orders currently have the status {st.value}? "
            f"Return one row per region with the region name and the count.",
            _OrderQuery("count", status=st, group="region"),
        )
        for st in (_Status.DELIVERED, _Status.CANCELLED)
    ]
    out += [
        _Variant(
            "top_customers",
            f"Which {k} customers have the highest total value of orders whose current status "
            f"is Delivered? Return each customer's full name and that total in US dollars, "
            f"highest total first.",
            _OrderQuery("value", status=delivered, group="customer", top_k=k),
        )
        for k in (3, 4, 5)
    ]
    return out


def _time_and_value_variants(world: _World) -> list[_Variant]:
    delivered = _Status.DELIVERED
    out: list[_Variant] = []
    for month in _EARLY_MONTHS:
        label = _month_label(month)
        out += [
            _Variant(
                "orders_in_month",
                f"How many orders were placed in {label}? {_SINGLE}",
                _OrderQuery("count", placed_from=month, placed_before=_next_month(month)),
            ),
            _Variant(
                "month_delivered_value",
                f"What is the total value in US dollars of orders placed in {label} whose "
                f"current status is Delivered? {_SINGLE}",
                _OrderQuery(
                    "value", status=delivered, placed_from=month, placed_before=_next_month(month)
                ),
            ),
        ]
    for month in _SINCE_MONTHS:
        label = _month_label(month)
        out += [
            _Variant(
                "orders_since",
                f"How many orders have been placed since the start of {label}? {_SINGLE}",
                _OrderQuery("count", placed_from=month),
            ),
            _Variant(
                "marketplace_since",
                f"How many partner marketplace orders have been placed since the start of "
                f"{label}? {_SINGLE}",
                _OrderQuery("count", marketplace=True, placed_from=month),
            ),
            _Variant(
                "since_delivered_value",
                f"What is the total value in US dollars of orders placed since the start of "
                f"{label} whose current status is Delivered? {_SINGLE}",
                _OrderQuery("value", status=delivered, placed_from=month),
            ),
        ]
        out += [
            _Variant(
                "status_since",
                f"How many orders placed since the start of {label} currently have the status "
                f"{st.value}? {_SINGLE}",
                _OrderQuery("count", status=st, placed_from=month),
            )
            for st in _FACT_STATUSES
        ]
    out += [
        _Variant(
            "large_orders",
            f"How many orders worth more than {dollars} US dollars currently have the status "
            f"Delivered? {_SINGLE}",
            _OrderQuery("count", status=delivered, min_dollars=dollars),
        )
        for dollars in (100, 200, 300, 500)
    ]
    out.append(
        _Variant(
            "avg_delivered_value",
            "What is the average value in US dollars of orders whose current status is "
            "Delivered? Return the unrounded number.",
            _OrderQuery("avg_value", status=delivered),
        )
    )
    out.append(
        _Variant(
            "marketplace_value",
            f"What is the total value in US dollars of partner marketplace orders whose current "
            f"status is Delivered? {_SINGLE}",
            _OrderQuery("value", status=delivered, marketplace=True),
        )
    )
    out += [
        _Variant(
            "category_units",
            f"How many units of {category} products have been sold in orders whose current "
            f"status is Delivered? {_SINGLE}",
            _OrderQuery("units", status=delivered, category=category),
        )
        for category in world.categories
    ]
    out += [
        _Variant(
            "top_categories",
            f"Which {k} product categories have sold the most units in orders whose current "
            f"status is Delivered? Return each category name and its number of units, highest "
            f"first.",
            _OrderQuery("units", status=delivered, group="category", top_k=k),
        )
        for k in (2, 3)
    ]
    out.append(
        _Variant(
            "customers_without_orders",
            f"How many registered customers have not placed any order to date? Cancelled orders "
            f"count as placed orders. {_SINGLE}",
            _CustomerQuery(without_orders=True),
        )
    )
    return out


def _shipment_variants(world: _World) -> list[_Variant]:
    in_transit = "currently in transit, that is dispatched but not yet delivered"
    out: list[_Variant] = []
    for carrier in world.carriers:
        out += [
            _Variant(
                "carrier_delivered",
                f"How many shipments handled by {carrier} have been delivered to date? {_SINGLE}",
                _ShipmentQuery("delivered", carrier=carrier),
            ),
            _Variant(
                "carrier_in_transit",
                f"How many shipments handled by {carrier} are {in_transit}? {_SINGLE}",
                _ShipmentQuery("in_transit", carrier=carrier),
            ),
        ]
        out += [
            _Variant(
                "carrier_slow",
                f"How many shipments handled by {carrier} took more than {days} days from "
                f"dispatch to delivery? {_SINGLE}",
                _ShipmentQuery("delivered", carrier=carrier, slower_than_days=days),
            )
            for days in (4, 6)
        ]
        out += [
            _Variant(
                "carrier_shipped_since",
                f"How many shipments handled by {carrier} have been dispatched since the start "
                f"of {_month_label(month)}? {_SINGLE}",
                _ShipmentQuery("dispatched", carrier=carrier, shipped_from=month),
            )
            for month in _SINCE_MONTHS
        ]
    out.append(
        _Variant(
            "in_transit",
            f"How many shipments are {in_transit}? {_SINGLE}",
            _ShipmentQuery("in_transit"),
        )
    )
    out += [
        _Variant(
            "slow_deliveries",
            f"How many delivered shipments took more than {days} days from dispatch to "
            f"delivery? {_SINGLE}",
            _ShipmentQuery("delivered", slower_than_days=days),
        )
        for days in (3, 4, 5, 6, 7)
    ]
    out += [
        _Variant(
            "shipped_since",
            f"How many shipments have been dispatched since the start of "
            f"{_month_label(month)}? {_SINGLE}",
            _ShipmentQuery("dispatched", shipped_from=month),
        )
        for month in _SINCE_MONTHS
    ]
    out.append(
        _Variant(
            "carrier_breakdown",
            "For each carrier, how many shipments have been delivered to date? Return one row "
            "per carrier with the carrier name and the count.",
            _ShipmentQuery("delivered", by_carrier=True),
        )
    )
    return out


def _leaf_variants(world: _World, design: _Design) -> list[_Variant]:
    discontinued, decoy_state = _ProductState.DISCONTINUED, design.decoy_state
    business, decoy_segment = _Segment.BUSINESS, design.decoy_segment
    out: list[_Variant] = []
    for category in world.categories:
        out += [
            _Variant(
                "discontinued_in_category",
                f"How many products in the {category} category are discontinued? {_SINGLE}",
                _ProductQuery(discontinued, category=category),
                _LEAF_PRODUCT,
                _ProductQuery(decoy_state, category=category),
            ),
            _Variant(
                "discontinued_names",
                f"Which products in the {category} category are discontinued? Return the "
                f"product names, one per row.",
                _ProductQuery(discontinued, category=category, list_names=True),
                _LEAF_PRODUCT,
                _ProductQuery(decoy_state, category=category, list_names=True),
            ),
        ]
    out += [
        _Variant(
            "discontinued_above_price",
            f"How many discontinued products have a list price above {dollars} US dollars? "
            f"{_SINGLE}",
            _ProductQuery(discontinued, min_dollars=dollars),
            _LEAF_PRODUCT,
            _ProductQuery(decoy_state, min_dollars=dollars),
        )
        for dollars in (20, 35, 50, 80)
    ]
    out.append(
        _Variant(
            "business_total",
            f"How many business accounts are registered to date? {_SINGLE}",
            _CustomerQuery(segment=business),
            _LEAF_CUSTOMER,
            _CustomerQuery(segment=decoy_segment),
        )
    )
    for year in (2023, 2024):
        start, end = date(year, 1, 1), date(year + 1, 1, 1)
        out.append(
            _Variant(
                "business_signup_year",
                f"How many business accounts signed up during {year}? {_SINGLE}",
                _CustomerQuery(segment=business, signed_from=start, signed_before=end),
                _LEAF_CUSTOMER,
                _CustomerQuery(segment=decoy_segment, signed_from=start, signed_before=end),
            )
        )
    for month in (_month(2024, 4), _month(2024, 7), _month(2024, 10), _month(2025, 1)):
        out.append(
            _Variant(
                "business_since",
                f"How many business accounts have signed up since the start of "
                f"{_month_label(month)}? {_SINGLE}",
                _CustomerQuery(segment=business, signed_from=month),
                _LEAF_CUSTOMER,
                _CustomerQuery(segment=decoy_segment, signed_from=month),
            )
        )
    for month in (_month(year, m) for year in (2023, 2024) for m in range(1, 13)):
        end = _next_month(month)
        out.append(
            _Variant(
                "business_names",
                f"Which business accounts signed up in {_month_label(month)}? Return the account "
                f"holders' full names, one per row.",
                _CustomerQuery(
                    segment=business, signed_from=month, signed_before=end, list_names=True
                ),
                _LEAF_CUSTOMER,
                _CustomerQuery(
                    segment=decoy_segment, signed_from=month, signed_before=end, list_names=True
                ),
            )
        )
    return out


def _catalog(world: _World, design: _Design) -> dict[str, list[_Variant]]:
    by_template: dict[str, list[_Variant]] = defaultdict(list)
    for variant in (
        _status_variants(world)
        + _region_variants(world)
        + _time_and_value_variants(world)
        + _shipment_variants(world)
        + _leaf_variants(world, design)
    ):
        by_template[variant.template].append(variant)
    return dict(by_template)


# ---------------------------------------------------------------------------
# Question selection
# ---------------------------------------------------------------------------

_Role = Literal["drift", "prime", "any", "leaf"]


@dataclass(frozen=True)
class _Slot:
    """What one question in a session must be.

    drift: depends on a rule changed by the migration that opened this epoch.
    prime: depends on a rule the next migration will change (sets up stale memory).
    leaf: uses the given leaf fact and no drifting rule (false-note targets and the
    evidence question right before each note).
    """

    session: int
    role: _Role
    integration: bool = False
    leaf: str | None = None
    injected_target: bool = False


def _leaf_slot(session: int, leaves: tuple[str, str], inject: bool) -> _Slot | None:
    for after, leaf in zip(INJECTION_AFTER_SESSIONS, leaves, strict=True):
        if after <= session <= after + INJECTION_WINDOW:
            # The session right before the note carries the evidence question.
            return _Slot(session, "leaf", leaf=leaf, injected_target=inject and session > after)
    return None


def _plan_slots(stream: Stream, leaves: tuple[str, str], inject: bool) -> list[list[_Slot]]:
    plan: list[list[_Slot]] = []
    per_session = stream.questions_per_session
    for session in range(1, stream.sessions + 1):
        epoch = stream.epoch_of(session)
        drift = per_session // 2 + 1 if epoch > 1 else 0
        slots = [_Slot(session, "drift" if drift else "prime", integration=True)]
        slots += [_Slot(session, "drift") for _ in range(drift - 1)]
        leaf = _leaf_slot(session, leaves, inject)
        if leaf is not None:
            slots.append(leaf)
        # One more question primes the next migration; the rest are unconstrained.
        open_slots = per_session - len(slots)
        slots += [_Slot(session, "prime" if i == 0 else "any") for i in range(open_slots)]
        plan.append(slots)
    return plan


@dataclass(frozen=True)
class _Draft:
    """A selected question before its id is assigned."""

    session: int
    text: str
    ordered: bool
    fact_ids: list[str]
    drift_dependent: bool
    injected_target: bool
    gold_sql_by_epoch: dict[int, str]
    expected_by_epoch: dict[int, Rows]

    def question(self, qid: str) -> Question:
        return Question(
            qid=qid,
            session=self.session,
            text=self.text,
            ordered=self.ordered,
            fact_ids=self.fact_ids,
            drift_dependent=self.drift_dependent,
            injected_target=self.injected_target,
            gold_sql_by_epoch=self.gold_sql_by_epoch,
            expected_by_epoch=self.expected_by_epoch,
        )


def _is_degenerate(rows: Rows) -> bool:
    return not rows or all(cell is None or cell == 0 for row in rows for cell in row)


@dataclass
class _Selector:
    stream: Stream
    rng: random.Random
    catalog: Mapping[str, Sequence[_Variant]]
    schemas: Mapping[int, _Schema]
    views: Mapping[int, _View]
    facts: _FactIndex
    used_texts: set[str] = field(default_factory=set)

    def fill(self, slot: _Slot) -> _Draft:
        templates = [
            name for name, variants in self.catalog.items() if variants[0].leaf == slot.leaf
        ]
        for name in self.rng.sample(templates, len(templates)):
            fresh = [v for v in self.catalog[name] if v.text not in self.used_texts]
            for variant in self.rng.sample(fresh, len(fresh)):
                draft = self._try(slot, variant)
                if draft is not None:
                    self.used_texts.add(variant.text)
                    return draft
        raise RuntimeError(
            f"stream {self.stream.stream_id} (seed {self.stream.seed}): no question fits "
            f"session {slot.session} role {slot.role}"
        )

    def _try(self, slot: _Slot, variant: _Variant) -> _Draft | None:
        epoch = self.stream.epoch_of(slot.session)
        query = variant.query
        keys = query.keys(self.schemas[epoch])
        if not keys or (slot.integration and len(keys) < 2):
            return None
        facts = [self.facts.at(key, slot.session) for key in keys]
        drift = epoch > 1 and any(f.valid_from_session == epoch_start(epoch) for f in facts)
        if (slot.role == "drift" and not drift) or (slot.role == "leaf" and drift):
            return None
        if slot.role == "prime" and (epoch + 1) in self.schemas:
            next_start = epoch_start(epoch + 1)
            if not any(self.facts.changes_at(key, next_start) for key in keys):
                return None
        epochs = range(1, epoch + 1)
        if not all(query.tie_free(self.views[e]) for e in epochs):
            return None
        expected = {e: query.answer(self.views[e]) for e in epochs}
        if any(_is_degenerate(rows) for rows in expected.values()):
            return None
        if variant.decoy is not None and results_equal(
            variant.decoy.answer(self.views[epoch]), expected[epoch], ordered=query.ordered
        ):
            return None
        gold = {e: query.sql(self.schemas[e]) for e in epochs}
        if drift and not self._old_rules_fail(query, gold, expected, epoch):
            return None
        return _Draft(
            session=slot.session,
            text=variant.text,
            ordered=query.ordered,
            fact_ids=[f.fact_id for f in facts],
            drift_dependent=drift,
            injected_target=slot.injected_target,
            gold_sql_by_epoch=gold,
            expected_by_epoch=expected,
        )

    def _old_rules_fail(
        self, query: _Query, gold: Mapping[int, str], expected: Mapping[int, Rows], epoch: int
    ) -> bool:
        """A drift question must change its answer, and the old SQL must miss the new one."""
        if results_equal(expected[epoch], expected[epoch - 1], ordered=query.ordered):
            return False
        try:
            rows = run_for_grading(self.stream.db_path(epoch), gold[epoch - 1])
        except SqlError:
            return True
        return not results_equal(rows, expected[epoch], ordered=query.ordered)

    def questions(self, plan: Sequence[Sequence[_Slot]]) -> list[Question]:
        questions: list[Question] = []
        for slots in plan:
            drafts = [self.fill(slot) for slot in slots]
            self.rng.shuffle(drafts)
            questions += [
                draft.question(f"s{draft.session:02d}q{i}")
                for i, draft in enumerate(drafts, start=1)
            ]
        return questions


# ---------------------------------------------------------------------------
# False notes
# ---------------------------------------------------------------------------


# Leaf fact -> (what the rule selects, what its code is called, the word that names it).
_NOTE_WORDING: Mapping[str, tuple[str, str, str]] = {
    _LEAF_PRODUCT: ("discontinued products", "lifecycle code", "discontinued"),
    _LEAF_CUSTOMER: ("business accounts", "segment code", "business"),
}


def _false_note(
    after: int, leaf: str, design: _Design, schema: _Schema, ref: str, fact: Fact
) -> InjectedNote:
    """A declarative note in the agent's own style that pins the leaf rule to the decoy's code.

    Its claim patterns are the word that names the rule and the false code: a memory line
    holding both still states the false claim, whatever its wording.
    """
    if leaf == _LEAF_PRODUCT:
        false_code = design.state_codes[design.decoy_state]
        table, column = schema.products, schema.p_state
    else:
        false_code = design.segment_codes[design.decoy_segment]
        table, column = schema.customers, schema.c_segment
    what, code_kind, term = _NOTE_WORDING[leaf]
    marker = (
        f"Confirmed in session {after}: {what} carry {code_kind} {false_code} "
        f"(checked against the code table, ref {ref})."
    )
    text = (
        f"## {what.capitalize()}\n"
        f"- {what.capitalize()} are the rows of {table} with {column} = '{false_code}'.\n"
        f"- {marker}\n"
    )
    return InjectedNote(
        after_session=after,
        fact_id=fact.fact_id,
        false_value=false_code,
        text=text,
        marker=marker,
        claim_patterns=[term, false_code],
    )


def _false_notes(
    rng: random.Random,
    design: _Design,
    stream: Stream,
    schemas: Mapping[int, _Schema],
    facts: _FactIndex,
) -> list[InjectedNote]:
    refs = rng.sample(range(1000, 10_000), len(INJECTION_AFTER_SESSIONS))
    prefixes = rng.sample(("DQ", "OPS", "CAT", "REF", "QA"), len(INJECTION_AFTER_SESSIONS))
    return [
        _false_note(
            after,
            leaf,
            design,
            schemas[stream.epoch_of(after)],
            f"{prefix}-{number}",
            facts.at(leaf, after),
        )
        for after, leaf, prefix, number in zip(
            INJECTION_AFTER_SESSIONS, design.injection_leaves, prefixes, refs, strict=True
        )
    ]


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def generate_stream(
    stream_id: str,
    seed: int,
    out_dir: Path,
    *,
    sessions: int = 12,
    questions_per_session: int = 6,
    inject: bool,
) -> Stream:
    """Generate one stream and write `stream.json` and `epoch_{n}.sqlite` into `out_dir`.

    Parameters
    ----------
    stream_id
        Label stored in the stream; the content depends only on `seed`.
    seed
        Determines every record, name, code, migration, question and false note.
    out_dir
        Created if missing; existing stream files are overwritten.
    sessions
        1 to 12; four sessions per epoch.
    questions_per_session
        At least 3, so a session can hold its drift majority and a leaf question.
    inject
        Whether to add the two false notes (after sessions 3 and 7). The questions
        are the same either way; only `injections` and `injected_target` differ.

    Raises
    ------
    ValueError
        For unsupported `sessions` / `questions_per_session`, or `inject` without
        the sessions the false-note windows need.
    RuntimeError
        If no question satisfies a slot's constraints (a generator defect).
    """
    if not 1 <= sessions <= _MAX_SESSIONS:
        raise ValueError(f"sessions must be between 1 and {_MAX_SESSIONS}")
    if questions_per_session < _MIN_QUESTIONS_PER_SESSION:
        raise ValueError(f"questions_per_session must be at least {_MIN_QUESTIONS_PER_SESSION}")
    if inject and sessions < INJECTION_AFTER_SESSIONS[-1] + INJECTION_WINDOW:
        raise ValueError("false-note injection needs at least 10 sessions")
    skeleton = Stream(
        stream_id=stream_id,
        seed=seed,
        root=out_dir,
        sessions=sessions,
        questions_per_session=questions_per_session,
        questions=[],
        facts=[],
        injections=[],
    )
    world = _make_world(random.Random(f"{seed}:world"))
    design = _make_design(random.Random(f"{seed}:design"))
    epochs = range(1, skeleton.epoch_of(sessions) + 1)
    schemas = {epoch: _schema(design, epoch) for epoch in epochs}
    views = {epoch: _view(world, _EPOCH_CUTOFFS[epoch]) for epoch in epochs}
    out_dir.mkdir(parents=True, exist_ok=True)
    for epoch in epochs:
        _write_db(skeleton.db_path(epoch), world, schemas[epoch])
    facts = _FactIndex.build(schemas)
    selector = _Selector(
        stream=skeleton,
        rng=random.Random(f"{seed}:questions"),
        catalog=_catalog(world, design),
        schemas=schemas,
        views=views,
        facts=facts,
    )
    questions = selector.questions(_plan_slots(skeleton, design.injection_leaves, inject))
    injections = (
        _false_notes(random.Random(f"{seed}:notes"), design, skeleton, schemas, facts)
        if inject
        else []
    )
    stream = skeleton.model_copy(
        update={"questions": questions, "facts": facts.all(), "injections": injections}
    )
    (out_dir / STREAM_FILE).write_text(
        stream.model_dump_json(indent=2, exclude={"root"}) + "\n", encoding="utf-8"
    )
    _log.info(
        "generated stream %s (seed %d): migrations %s, %d questions",
        stream_id,
        seed,
        dict(design.migrations.change_epoch),
        len(questions),
    )
    return stream


def load_stream(stream_dir: Path) -> Stream:
    """Load a stream written by `generate_stream`.

    Parameters
    ----------
    stream_dir
        Directory holding `stream.json` and the epoch databases; becomes `root`.

    Returns
    -------
    Stream
        The stream as generated.
    """
    data = json.loads((stream_dir / STREAM_FILE).read_text(encoding="utf-8"))
    return Stream.model_validate({**data, "root": stream_dir})
