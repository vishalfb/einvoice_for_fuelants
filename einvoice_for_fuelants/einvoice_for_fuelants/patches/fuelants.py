
import json

import jwt

import frappe
from frappe import _
from frappe.utils import (
    add_to_date,
    cstr,
    format_date,
    get_datetime,
    getdate,
    random_string,
)
from india_compliance.gst_india.utils.e_invoice import (EInvoiceData, log_e_invoice)
from india_compliance.exceptions import GatewayTimeoutError
from india_compliance.gst_india.api_classes.nic.e_invoice import EInvoiceAPI
from india_compliance.gst_india.constants import (
    CURRENCY_CODES,
    EXPORT_TYPES,
    GST_CATEGORIES,
    PORT_CODES,
)
from india_compliance.gst_india.constants.e_invoice import (
    CANCEL_REASON_CODES,
    ITEM_LIMIT,
)
from india_compliance.gst_india.utils import (
    are_goods_supplied,
    is_api_enabled,
    is_foreign_doc,
    is_overseas_doc,
    load_doc,
    parse_datetime,
    send_updated_doc,
    update_onload,
)
from india_compliance.gst_india.utils.e_waybill import (
    _cancel_e_waybill,
    log_and_process_e_waybill_generation,
)
from india_compliance.gst_india.utils.transaction_data import GSTTransactionData

def process_items_and_update_data(data):
    """
    This function processes items by filtering them based on HSN codes,
    calculating total amounts, and updating ValDtls and ItemList.
    """
    # Retrieve HSN codes from configuration
    filtered_hsn = set(frappe.conf.hsn_code_others)  # Use sets for O(1) lookups
    filtered_discount_hsn = set(frappe.conf.hsn_code_discount)

    # Separate filtered and discount items
    filtered_items, filtered_discount_items, remaining_items = [], [], []

    for item in data["ItemList"]:
        if item["HsnCd"] in filtered_hsn:
            filtered_items.append(item)
        elif item["HsnCd"] in filtered_discount_hsn:
            filtered_discount_items.append(item)
        else:
            remaining_items.append(item)

    # Calculate total amounts
    total_amount = sum(item["TotAmt"] for item in filtered_items)
    filtered_discount_amount = sum(item["TotAmt"] for item in filtered_discount_items)

    # Update ValDtls
    val_dtls = data["ValDtls"]
    adjustment = total_amount - filtered_discount_amount
    val_dtls["OthChrg"] += adjustment
    val_dtls["AssVal"] -= adjustment
    val_dtls["Discount"] = 0
    buyer_dtls = data["BuyerDtls"]
    data["ShipDtls"]["LglNm"] = buyer_dtls["LglNm"]
    data["ShipDtls"]["TrdNm"] = buyer_dtls["TrdNm"]
    if data["ShipDtls"]["Gstin"] == "URP":
        data["ShipDtls"]["Gstin"] = None

    # Update ItemList with remaining items and reassign SlNo
    data["ItemList"] = [{**item, "SlNo": str(idx)} for idx, item in enumerate(remaining_items, start=1)]
    #print("Updated ItemList: ", data["ItemList"])

    return data

@frappe.whitelist()
def generate_e_invoice(docname, throw=True, force=False):
    doc = load_doc("Sales Invoice", docname, "submit")
    frappe.logger().info(f"Loaded Sales Invoice: {frappe.as_json(doc)}")

    settings = frappe.get_cached_doc("GST Settings")

    try:
        if (
            not force
            and settings.enable_retry_einv_ewb_generation
            and settings.is_retry_einv_ewb_generation_pending
        ):
            raise GatewayTimeoutError

        # Fetch data from the document
        e_invoice_data  = EInvoiceData(doc).get_data()
        data = process_items_and_update_data(e_invoice_data)
        api = EInvoiceAPI.create(doc)
        result = api.generate_irn(data)

        # Handle Duplicate IRN
        if result.InfCd == "DUPIRN":
            response = api.get_e_invoice_by_irn(result.Desc.Irn)

            # Handle error 2283:
            # IRN details cannot be provided as it is generated more than 2 days ago
            result = result.Desc if response.error_code == "2283" else response

    except GatewayTimeoutError as e:
        einvoice_status = "Failed"

        if settings.enable_retry_einv_ewb_generation:
            einvoice_status = "Auto-Retry"
            settings.db_set(
                "is_retry_einv_ewb_generation_pending", 1, update_modified=False
            )

        doc.db_set({"einvoice_status": einvoice_status}, commit=True)

        frappe.msgprint(
            _(
                "Government services are currently slow, resulting in a Gateway Timeout error. We apologize for the inconvenience caused. Your e-invoice generation will be automatically retried every 5 minutes."
            ),
            _("Warning"),
            indicator="yellow",
        )

        raise e

    except frappe.ValidationError as e:
        doc.db_set({"einvoice_status": "Failed"})

        if throw:
            raise e

        frappe.clear_last_message()
        frappe.msgprint(
            _(
                "e-Invoice auto-generation failed with error:<br>{0}<br><br>"
                "Please rectify this issue and generate e-Invoice manually."
            ).format(str(e)),
            _("Warning"),
            indicator="yellow",
        )

        return

    except Exception as e:
        doc.db_set({"einvoice_status": "Failed"})
        raise e

    doc.db_set(
        {
            "irn": result.Irn,
            "einvoice_status": "Generated",
        }
    )

    invoice_data = None
    if result.SignedInvoice:
        decoded_invoice = json.loads(
            jwt.decode(result.SignedInvoice, options={"verify_signature": False})[
                "data"
            ]
        )
        invoice_data = frappe.as_json(decoded_invoice, indent=4)

    log_e_invoice(
        doc,
        {
            "irn": doc.irn,
            "sales_invoice": docname,
            "acknowledgement_number": result.AckNo,
            "acknowledged_on": parse_datetime(result.AckDt),
            "signed_invoice": result.SignedInvoice,
            "signed_qr_code": result.SignedQRCode,
            "invoice_data": invoice_data,
            "is_generated_in_sandbox_mode": api.sandbox_mode,
        },
    )

    if result.EwbNo:
        log_and_process_e_waybill_generation(doc, result, with_irn=True)

    if not frappe.request:
        return

    frappe.msgprint(
        _("e-Invoice generated successfully"),
        indicator="green",
        alert=True,
    )

    return send_updated_doc(doc)


