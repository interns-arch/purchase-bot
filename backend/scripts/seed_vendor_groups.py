"""Link the 43 existing vendor WhatsApp groups to their vendors (29 Oct 2026).
Idempotent -- run again any time:

    python -m backend.scripts.seed_vendor_groups

Matched by the vendor numbers in each group; "Ark Group (Ford)" (three Ark
companies) counts as Ark Automotive CT_AA (team's choice). JAIPUR WAREHOUSE
and CT - Parts Enquiry Group are not vendor groups and are left out."""

from __future__ import annotations

GROUPS = {
    "120363419911403061@g.us": ("CT_AVP", "CarTrends 🤝 Ark Vehicles (Toyota)"),
    "120363403257117032@g.us": ("CT_PRA", "CarTrends 🤝 P R Auto"),
    "120363330878014456@g.us": ("CT_BAP", "CarTrends 🤝 Brite Autocars (Noida)"),
    "120363405183817575@g.us": ("CT_JGP", "CarTrends 🤝 Jonquil Autozar"),
    "120363422013173386@g.us": ("CT_EAA", "CarTrends 🤝 Ess Aay (Mahindra, Delhi)"),
    "120363424825934995@g.us": ("CT_EAA", "CarTrends 🤝 Ess Aay (Mahindra, Noida)"),
    "120363422648213710@g.us": ("CT_RPC", "CarTrends 🤝 RSR Petro Chemicals"),
    "120363401220413312@g.us": ("CT_MSA", "CarTrends 🤝 Arihant Auto (Ford)"),
    "120363219649595933@g.us": ("CT_MSA", "CarTrends 🤝 Arihant Auto"),
    "120363417667516652@g.us": ("CT_ND", "CarTrends 🤝 Northend Distributors"),
    "120363404223628925@g.us": ("CT_MSP", "CarTrends 🤝 Mohan Spares"),
    "120363426280625765@g.us": ("CT_VCL", "CarTrends 🤝 Veedol"),
    "120363228026339833@g.us": ("CT_AA", "CarTrends 🤝 Ark Group (Ford)"),
    "120363406288549930@g.us": ("CT_AAS", "CarTrends 🤝 Alva Auto Spares"),
    "120363416497505618@g.us": ("CT_MSK", "CarTrends 🤝 Kashliwal Auto (Maruti)"),
    "120363171468375945@g.us": ("CT_FCI", "CarTrends 🤝 Frontier Cars"),
    "120363405395129857@g.us": ("CT_AIP", "CarTrends 🤝 Ajit Industries (Abro)"),
    "120363409962577117@g.us": ("CT_AIP", "CarTrends 🤝 Ajit Industries (Abro 2)"),
    "120363403926450394@g.us": ("CT_SBI", "CarTrends 🤝 Sangsin Brake (Jaipur)"),
    "120363426229966550@g.us": ("CT_AMC", "CarTrends 🤝 ANV Marketing (WOW Trexim)"),
    "120363401282352420@g.us": ("CT_SMC", "CarTrends 🤝 Surya Motor"),
    "120363430088927145@g.us": ("CT_SMC", "CarTrends 🤝 Surya Motor (2)"),
    "120363426848966129@g.us": ("CT_SMC", "CarTrends 🤝 Surya Motor (Aftermarket)"),
    "120363408225003819@g.us": ("CT_GAPL", "CarTrends 🤝 Ganganagar Automobiles (Honda)"),
    "120363428264053566@g.us": ("CT_MLA", "CarTrends 🤝 Mithan Lal Agencies"),
    "120363408979281952@g.us": ("CT_JBD", "CarTrends 🤝 J.B. Distributors"),
    "120363403376249184@g.us": ("CT_JBD", "CarTrends 🤝 Shree Balaji Enterprises (Bills)"),
    "120363346115146347@g.us": ("CT_KAC", "CarTrends 🤝 KRS Auto (RK Rane)"),
    "120363410538478906@g.us": ("CT_AOT", "CarTrends 🤝 A-One Trading (Hyundai)"),
    "120363404119053050@g.us": ("CT_CC", "CarTrends 🤝 Car Clan (Tata)"),
    "120363421360830684@g.us": ("CT_SSM", "CarTrends 🤝 Shree Shyam Motors (Tata)"),
    "120363425060706248@g.us": ("CT_SB", "CarTrends 🤝 Siyaram Brothers (Magsol)"),
    "120363322052040485@g.us": ("CT_GAP", "CarTrends 🤝 GAPL Automotive"),
    "120363409875474332@g.us": ("CT_GMC", "CarTrends 🤝 Goyal Motors (Aftermarket)"),
    "120363421744703570@g.us": ("CT_KA", "CarTrends 🤝 Krishna Associates"),
    "120363420201999840@g.us": ("CT_GAPA", "CarTrends 🤝 Gaurav Auto Products"),
    "120363422668573715@g.us": ("CT_S4", "CarTrends 🤝 Spares 4U"),
    "120363418200301007@g.us": ("CT_SM", "CarTrends 🤝 Saisha Motors"),
    "120363427937298609@g.us": ("CT_SM", "CarTrends 🤝 Saisha Motors (Skoda)"),
    "120363409384066653@g.us": ("CT_RSH", "CarTrends 🤝 Rajasthan Spare House (Tata, Hissar)"),
    "120363406756935082@g.us": ("CT_AI", "CarTrends 🤝 Amriksons (Sachs)"),
    "120363409590365671@g.us": ("CT_KAS", "CarTrends 🤝 Kapoor Auto Sales (Accurate)"),
    "120363408069395278@g.us": ("CT_ALSPLM", "CarTrends 🤝 Atlantic Lubricants (Motul)"),
}


def main() -> None:
    from backend.app.integrations.whatsapp.models import WhapiVendorGroup
    from core.db import get_session

    with get_session() as session:
        for group_id, (code, name) in GROUPS.items():
            session.merge(WhapiVendorGroup(group_id=group_id, vendor_code=code, name=name, created_by_bot=False))
    print(f"{len(GROUPS)} vendor groups linked.")


if __name__ == "__main__":
    main()
