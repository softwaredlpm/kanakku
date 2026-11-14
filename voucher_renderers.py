# voucher_renderers.py - Voucher print layouts for Kanakku
import json
import urllib.parse

def build_inventory_table_rows(items, item_batches=None):
    if item_batches is None:
        item_batches = {}
    item_rows = ""
    total_qty = 0.0
    total_amt = 0.0
    if items:
        for idx, it in enumerate(items, 1):
            q = abs(it.get('Qty', 0))
            p = abs(it.get('Price', 0))
            a = abs(it.get('Amount', 0))
            total_qty += q
            total_amt += a
            mcode = it.get('MasterCode1', 0)

            batches = item_batches.get(mcode, []) if item_batches else []
            batch_sub = ""
            if batches:
                batch_sub = '<div style="margin-top: 3px; font-size: 9.5px; color: #444; line-height: 1.35; padding-left: 4px;">'
                for b in batches:
                    desc = f" [{b['BatchDesc']}]" if b.get('BatchDesc') else ""
                    batch_sub += f"<div>&bull; Bale {b['BatchNo']}: {b['BatchQty']:g} {it.get('UnitName', '')}{desc}</div>"
                batch_sub += '</div>'

            rate_str = f"{p:,.2f}" if p > 0 else "-"
            amt_str = f"{a:,.2f}" if a > 0 else "-"

            item_rows += f'''
            <tr style="border-bottom: 1px solid #000; font-size: 11px;">
                <td class="tbl-bordered" style="padding: 5px 4px; text-align: center; vertical-align: top;">{idx}</td>
                <td class="tbl-bordered" style="padding: 5px 8px; vertical-align: top;">
                    <span style="font-weight: bold; font-size: 12px; color: #000;">{it.get('ItemName', '')}</span>
                    {batch_sub}
                </td>
                <td class="tbl-bordered" style="padding: 5px 4px; text-align: center; vertical-align: top;">{it.get('HSNCode') or '-'}</td>
                <td class="tbl-bordered" style="padding: 5px 8px; text-align: right; vertical-align: top; font-weight: bold;">{q:,.2f}</td>
                <td class="tbl-bordered" style="padding: 5px 4px; text-align: center; vertical-align: top;">{it.get('UnitName', 'Pcs.')}</td>
                <td class="tbl-bordered" style="padding: 5px 8px; text-align: right; vertical-align: top;">{rate_str}</td>
                <td class="tbl-bordered" style="padding: 5px 8px; text-align: right; vertical-align: top; font-weight: bold;">{amt_str}</td>
            </tr>
            '''

    spacer_count = max(0, 5 - len(items)) if items else 5
    spacer_rows = ""
    for _ in range(spacer_count):
        spacer_rows += '''<tr style="height: 22px; border-bottom: 1px solid #eee;">
            <td class="tbl-bordered">&nbsp;</td>
            <td class="tbl-bordered">&nbsp;</td>
            <td class="tbl-bordered">&nbsp;</td>
            <td class="tbl-bordered">&nbsp;</td>
            <td class="tbl-bordered">&nbsp;</td>
            <td class="tbl-bordered">&nbsp;</td>
            <td class="tbl-bordered">&nbsp;</td>
        </tr>'''
    return item_rows, spacer_rows, total_qty, total_amt


def render_material_issue_html(header, party, comp, p_cfg, from_mc, to_mc, items, transport, item_batches, meta, css):
    vch_no = header.get('VchNo', '').strip()
    date = header.get('Date', '')
    series_name = header.get('SeriesName', '')
    party_name = party.get('PrintName') or party.get('Name') or header.get('PartyName') or 'Job Worker'
    party_addr = f"{party.get('Address1', '')} {party.get('Address2', '')}".strip()
    party_gstin = party.get('GSTNo') or 'Unregistered'

    item_rows, spacer_rows, total_qty, total_amt = build_inventory_table_rows(items, item_batches)

    amount_box_html = ""
    if total_amt > 0:
        amount_box_html = f'''
        <div style="padding: 6px 10px; border-bottom: 1.5px solid #000; background: #fafafa; font-size: 11px;">
            <strong>Estimated Value (in words):</strong><br>
            <span style="font-size: 12px; font-weight: bold; color: #1e3a8a;">{header.get('AmountInWords', '')}</span>
        </div>
        '''

    return f'''<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>MATERIAL ISSUE CHALLAN - {vch_no}</title>
    <style>{css}</style>
</head>
<body>
    <div class="action-bar no-print">
        <button onclick="window.history.back();" class="btn btn-back">&#x2190; Back</button>
        <button onclick="window.print();" class="btn btn-print">&#x1F5B6; Print / Save PDF</button>
        <button onclick="shareWhatsApp();" class="btn btn-wa">&#x1F4AC; WhatsApp</button>
    </div>

    <div class="mobile-hint no-print">&#x1F4F1; Swipe left/right to view full challan &bull; Pinch to zoom</div>
    <div class="voucher-scroll-wrap">
        <div class="voucher-box">
        <!-- TOP TITLE & COPY BAR -->
        <table style="width: 100%; border-collapse: collapse; border-bottom: 1.5px solid #000; font-size: 11px;">
            <tr>
                <td style="padding: 4px 8px; width: 33%; font-weight: bold;">{f'GSTIN: {meta["gstin"]}' if meta['gstin'] else ''}</td>
                <td style="padding: 4px 8px; width: 34%; text-align: center; font-size: 13px; font-weight: bold; letter-spacing: 1px;">MATERIAL ISSUE CHALLAN</td>
                <td style="padding: 4px 8px; width: 33%; text-align: right; font-weight: bold;">(JOB WORKER COPY)</td>
            </tr>
        </table>

        <!-- COMPANY HEADER -->
        <div style="text-align: center; padding: 8px 12px; border-bottom: 1.5px solid #000;">
            <div style="font-size: 22px; font-weight: bold; letter-spacing: 0.5px;">{meta['company_name']}</div>
            {f'<div style="font-size: 11px; font-weight: bold; margin-top: 2px;">{meta["addr1"]}</div>' if meta['addr1'] else ''}
            {f'<div style="font-size: 11px;">{meta["addr2"]}</div>' if meta['addr2'] else ''}
            <div style="font-size: 11px; margin-top: 3px;">
                {f'Mobile: <strong>{meta["mobile"]}</strong> | ' if meta['mobile'] else ''}State: <strong>TAMIL NADU (Code: 33)</strong>
            </div>
            <div style="font-size: 9.5px; color: #555; margin-top: 2px;">(Issued Under Rule 45 of CGST Rules - Goods for Processing / Job Work)</div>
        </div>

        <!-- JOB WORK ISSUE DETAILS -->
        <table style="width: 100%; border-collapse: collapse; border-bottom: 1.5px solid #000; font-size: 11px;">
            <tr>
                <!-- Left: Source & Destination -->
                <td style="width: 55%; border-right: 1.5px solid #000; padding: 8px 10px; vertical-align: top;">
                    <div style="font-size: 10px; font-weight: bold; text-transform: uppercase; color: #b45309; margin-bottom: 4px; letter-spacing: 0.5px;">
                        &#x25B6; ISSUED FROM (மூலக் கிடங்கு):
                    </div>
                    <div style="font-size: 13px; font-weight: bold; color: #1e293b; margin-bottom: 8px; padding-left: 4px;">
                        {from_mc or 'MAIN STORE'}
                    </div>

                    <div style="font-size: 10px; font-weight: bold; text-transform: uppercase; color: #1e40af; margin-bottom: 4px; letter-spacing: 0.5px;">
                        &#x25BC; ISSUED TO (JOB WORKER / நபர் விபரம்):
                    </div>
                    <div style="font-size: 13px; font-weight: bold; color: #000; padding-left: 4px;">
                        {party_name}
                    </div>
                    {f'<div style="padding-left: 4px; color: #475569; font-size: 10px;">{party_addr}</div>' if party_addr else ''}
                    <div style="padding-left: 4px; color: #475569; font-size: 10px;">GSTIN: <strong>{party_gstin}</strong></div>
                </td>

                <!-- Right: Meta -->
                <td style="width: 45%; padding: 8px 10px; vertical-align: top;">
                    <table style="width: 100%; border-collapse: collapse; font-size: 11px; line-height: 1.6;">
                        <tr>
                            <td style="width: 45%; font-weight: bold;">Challan No.:</td>
                            <td style="font-weight: bold; font-size: 13px; color: #1e3a8a;">{vch_no}</td>
                        </tr>
                        <tr>
                            <td style="font-weight: bold;">Dated:</td>
                            <td style="font-weight: bold;">{date}</td>
                        </tr>
                        {f'<tr><td style="font-weight: bold;">Series:</td><td style="font-weight: bold;">{series_name}</td></tr>' if series_name else ''}
                        <tr>
                            <td style="font-weight: bold;">Transport:</td>
                            <td>{transport.get('Transport') or '-'}</td>
                        </tr>
                        <tr>
                            <td style="font-weight: bold;">Vehicle No.:</td>
                            <td>{transport.get('VehicleNo') or '-'}</td>
                        </tr>
                        <tr>
                            <td style="font-weight: bold;">Station / City:</td>
                            <td>{transport.get('Station') or '-'}</td>
                        </tr>
                    </table>
                </td>
            </tr>
        </table>

        <!-- ITEMS GRID -->
        <table class="tbl-bordered" style="width: 100%; border-collapse: collapse; border-bottom: 1.5px solid #000;">
            <thead>
                <tr style="background: #f8fafc; border-bottom: 1.5px solid #000; font-size: 11px; font-weight: bold;">
                    <th class="tbl-bordered" style="padding: 5px; width: 5%; text-align: center;">S.N.</th>
                    <th class="tbl-bordered" style="padding: 5px 8px; text-align: left;">Description of Goods / Fabric / Semi-Finished</th>
                    <th class="tbl-bordered" style="padding: 5px 4px; width: 12%; text-align: center;">HSN/SAC</th>
                    <th class="tbl-bordered" style="padding: 5px 8px; width: 14%; text-align: right;">Quantity</th>
                    <th class="tbl-bordered" style="padding: 5px 4px; width: 8%; text-align: center;">Unit</th>
                    <th class="tbl-bordered" style="padding: 5px 8px; width: 12%; text-align: right;">Rate (&#x20B9;)</th>
                    <th class="tbl-bordered" style="padding: 5px 8px; width: 15%; text-align: right;">Estimated Val (&#x20B9;)</th>
                </tr>
            </thead>
            <tbody>
                {item_rows}
                {spacer_rows}
            </tbody>
            <tfoot>
                <tr style="font-weight: bold; background: #f8fafc; font-size: 11px; border-top: 1.5px solid #000;">
                    <td class="tbl-bordered" colspan="3" style="padding: 6px 8px; text-align: right;">TOTAL ISSUED:</td>
                    <td class="tbl-bordered" style="padding: 6px 8px; text-align: right; font-weight: bold;">{total_qty:,.2f}</td>
                    <td class="tbl-bordered" style="padding: 6px 4px; text-align: center;">Pcs.</td>
                    <td class="tbl-bordered" style="padding: 6px 8px; text-align: right;">-</td>
                    <td class="tbl-bordered" style="padding: 6px 8px; text-align: right; font-weight: bold;">{f'&#x20B9;{total_amt:,.2f}' if total_amt > 0 else '-'}</td>
                </tr>
            </tfoot>
        </table>

        {amount_box_html}

        <div style="padding: 6px 10px; font-size: 10px; color: #555; border-bottom: 1.5px solid #000; line-height: 1.4;">
            <strong>Terms & Job Work Conditions:</strong> 1. Goods issued solely for manufacturing / processing as Job Work under Rule 45 of CGST Rules. 2. Goods remain property of the company and must be returned with delivery challan after processing.
        </div>

        <!-- 3 SIGNATURE BLOCKS -->
        <table style="width: 100%; font-size: 11px;">
            <tr>
                <td style="width: 33.3%; padding: 10px 8px; vertical-align: top; border-right: 1px solid #ccc; text-align: center;">
                    <div style="font-size: 10px; color: #666; margin-bottom: 35px;">Goods Dispatched By</div>
                    <div style="border-top: 1px dashed #666; display: inline-block; padding-top: 3px; font-weight: bold; font-size: 10px;">Store Incharge</div>
                </td>
                <td style="width: 33.3%; padding: 10px 8px; vertical-align: top; border-right: 1px solid #ccc; text-align: center;">
                    <div style="font-size: 10px; color: #666; margin-bottom: 35px;">Transport / Carrier Signature</div>
                    <div style="border-top: 1px dashed #666; display: inline-block; padding-top: 3px; font-weight: bold; font-size: 10px;">Driver's Signature</div>
                </td>
                <td style="width: 33.4%; padding: 10px 8px; vertical-align: top; text-align: center;">
                    <div style="font-size: 10px; color: #666; margin-bottom: 35px;">Received By (Job Worker)</div>
                    <div style="border-top: 1px dashed #666; display: inline-block; padding-top: 3px; font-weight: bold; font-size: 10px;">Receiver's Signature & Seal</div>
                </td>
            </tr>
        </table>
        </div>
    </div>

    <script>
        function shareWhatsApp() {{
            const text = encodeURIComponent(
                "*{meta['company_name']} - MATERIAL ISSUE CHALLAN*\n" +
                "Challan No: {vch_no}\n" +
                "Date: {date}\n" +
                "Issued To: {party_name}\n" +
                "From MC: {from_mc or 'MAIN STORE'}\n" +
                "Total Qty: {total_qty:,.2f} Pcs.\n" +
                "View Challan: " + window.location.href
            );
            window.open("https://api.whatsapp.com/send?text=" + text, "_blank");
        }}
    </script>
</body>
</html>'''


def render_material_receipt_html(header, party, comp, p_cfg, from_mc, to_mc, items, transport, item_batches, meta, css):
    vch_no = header.get('VchNo', '').strip()
    date = header.get('Date', '')
    series_name = header.get('SeriesName', '')
    party_name = party.get('PrintName') or party.get('Name') or header.get('PartyName') or 'Job Worker'
    party_addr = f"{party.get('Address1', '')} {party.get('Address2', '')}".strip()
    party_gstin = party.get('GSTNo') or 'Unregistered'

    item_rows, spacer_rows, total_qty, total_amt = build_inventory_table_rows(items, item_batches)

    amount_box_html = ""
    if total_amt > 0:
        amount_box_html = f'''
        <div style="padding: 6px 10px; border-bottom: 1.5px solid #000; background: #fafafa; font-size: 11px;">
            <strong>Job Charges / Total Value (in words):</strong><br>
            <span style="font-size: 12px; font-weight: bold; color: #1e3a8a;">{header.get('AmountInWords', '')}</span>
        </div>
        '''

    return f'''<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>MATERIAL RECEIPT NOTE - {vch_no}</title>
    <style>{css}</style>
</head>
<body>
    <div class="action-bar no-print">
        <button onclick="window.history.back();" class="btn btn-back">&#x2190; Back</button>
        <button onclick="window.print();" class="btn btn-print">&#x1F5B6; Print / Save PDF</button>
        <button onclick="shareWhatsApp();" class="btn btn-wa">&#x1F4AC; WhatsApp</button>
    </div>

    <div class="mobile-hint no-print">&#x1F4F1; Swipe left/right to view full note &bull; Pinch to zoom</div>
    <div class="voucher-scroll-wrap">
        <div class="voucher-box">
        <!-- TOP TITLE & COPY BAR -->
        <table style="width: 100%; border-collapse: collapse; border-bottom: 1.5px solid #000; font-size: 11px;">
            <tr>
                <td style="padding: 4px 8px; width: 33%; font-weight: bold;">{f'GSTIN: {meta["gstin"]}' if meta['gstin'] else ''}</td>
                <td style="padding: 4px 8px; width: 34%; text-align: center; font-size: 13px; font-weight: bold; letter-spacing: 1px;">MATERIAL RECEIPT NOTE</td>
                <td style="padding: 4px 8px; width: 33%; text-align: right; font-weight: bold;">(GOODS INWARD RECORD)</td>
            </tr>
        </table>

        <!-- COMPANY HEADER -->
        <div style="text-align: center; padding: 8px 12px; border-bottom: 1.5px solid #000;">
            <div style="font-size: 22px; font-weight: bold; letter-spacing: 0.5px;">{meta['company_name']}</div>
            {f'<div style="font-size: 11px; font-weight: bold; margin-top: 2px;">{meta["addr1"]}</div>' if meta['addr1'] else ''}
            {f'<div style="font-size: 11px;">{meta["addr2"]}</div>' if meta['addr2'] else ''}
            <div style="font-size: 11px; margin-top: 3px;">
                {f'Mobile: <strong>{meta["mobile"]}</strong> | ' if meta['mobile'] else ''}State: <strong>TAMIL NADU (Code: 33)</strong>
            </div>
            <div style="font-size: 9.5px; color: #555; margin-top: 2px;">(Goods Inward from Job Worker / Processing)</div>
        </div>

        <!-- JOB WORK INWARD DETAILS -->
        <table style="width: 100%; border-collapse: collapse; border-bottom: 1.5px solid #000; font-size: 11px;">
            <tr>
                <!-- Left: Source & Destination -->
                <td style="width: 55%; border-right: 1.5px solid #000; padding: 8px 10px; vertical-align: top;">
                    <div style="font-size: 10px; font-weight: bold; text-transform: uppercase; color: #15803d; margin-bottom: 4px; letter-spacing: 0.5px;">
                        &#x25B6; RECEIVED FROM (JOB WORKER / நபர் விபரம்):
                    </div>
                    <div style="font-size: 13px; font-weight: bold; color: #000; padding-left: 4px;">
                        {party_name}
                    </div>
                    {f'<div style="padding-left: 4px; color: #475569; font-size: 10px;">{party_addr}</div>' if party_addr else ''}
                    <div style="padding-left: 4px; color: #475569; font-size: 10px;">GSTIN: <strong>{party_gstin}</strong></div>

                    <div style="font-size: 10px; font-weight: bold; text-transform: uppercase; color: #1e40af; margin-top: 8px; margin-bottom: 4px; letter-spacing: 0.5px;">
                        &#x25BC; RECEIVED INTO (இலக்குக் கிடங்கு):
                    </div>
                    <div style="font-size: 13px; font-weight: bold; color: #1e293b; padding-left: 4px;">
                        {to_mc or 'MAIN STORE'}
                    </div>
                </td>

                <!-- Right: Meta -->
                <td style="width: 45%; padding: 8px 10px; vertical-align: top;">
                    <table style="width: 100%; border-collapse: collapse; font-size: 11px; line-height: 1.6;">
                        <tr>
                            <td style="width: 45%; font-weight: bold;">Receipt No.:</td>
                            <td style="font-weight: bold; font-size: 13px; color: #15803d;">{vch_no}</td>
                        </tr>
                        <tr>
                            <td style="font-weight: bold;">Dated:</td>
                            <td style="font-weight: bold;">{date}</td>
                        </tr>
                        {f'<tr><td style="font-weight: bold;">Series:</td><td style="font-weight: bold;">{series_name}</td></tr>' if series_name else ''}
                        <tr>
                            <td style="font-weight: bold;">Inspection:</td>
                            <td>Passed Quality Control</td>
                        </tr>
                        <tr>
                            <td style="font-weight: bold;">Station:</td>
                            <td>{transport.get('Station') or '-'}</td>
                        </tr>
                    </table>
                </td>
            </tr>
        </table>

        <!-- ITEMS GRID -->
        <table class="tbl-bordered" style="width: 100%; border-collapse: collapse; border-bottom: 1.5px solid #000;">
            <thead>
                <tr style="background: #f8fafc; border-bottom: 1.5px solid #000; font-size: 11px; font-weight: bold;">
                    <th class="tbl-bordered" style="padding: 5px; width: 5%; text-align: center;">S.N.</th>
                    <th class="tbl-bordered" style="padding: 5px 8px; text-align: left;">Description of Finished / Processed Goods</th>
                    <th class="tbl-bordered" style="padding: 5px 4px; width: 12%; text-align: center;">HSN/SAC</th>
                    <th class="tbl-bordered" style="padding: 5px 8px; width: 14%; text-align: right;">Received Qty</th>
                    <th class="tbl-bordered" style="padding: 5px 4px; width: 8%; text-align: center;">Unit</th>
                    <th class="tbl-bordered" style="padding: 5px 8px; width: 12%; text-align: right;">Job Rate (&#x20B9;)</th>
                    <th class="tbl-bordered" style="padding: 5px 8px; width: 15%; text-align: right;">Job Amount (&#x20B9;)</th>
                </tr>
            </thead>
            <tbody>
                {item_rows}
                {spacer_rows}
            </tbody>
            <tfoot>
                <tr style="font-weight: bold; background: #f8fafc; font-size: 11px; border-top: 1.5px solid #000;">
                    <td class="tbl-bordered" colspan="3" style="padding: 6px 8px; text-align: right;">TOTAL RECEIVED:</td>
                    <td class="tbl-bordered" style="padding: 6px 8px; text-align: right; font-weight: bold;">{total_qty:,.2f}</td>
                    <td class="tbl-bordered" style="padding: 6px 4px; text-align: center;">Pcs.</td>
                    <td class="tbl-bordered" style="padding: 6px 8px; text-align: right;">-</td>
                    <td class="tbl-bordered" style="padding: 6px 8px; text-align: right; font-weight: bold;">{f'&#x20B9;{total_amt:,.2f}' if total_amt > 0 else '-'}</td>
                </tr>
            </tfoot>
        </table>

        {amount_box_html}

        <div style="padding: 6px 10px; font-size: 10px; color: #555; border-bottom: 1.5px solid #000; line-height: 1.4;">
            <strong>Verification Note:</strong> Goods received from processor in good condition, inspected, counted, and taken into stock registers.
        </div>

        <!-- 3 SIGNATURE BLOCKS -->
        <table style="width: 100%; font-size: 11px;">
            <tr>
                <td style="width: 33.3%; padding: 10px 8px; vertical-align: top; border-right: 1px solid #ccc; text-align: center;">
                    <div style="font-size: 10px; color: #666; margin-bottom: 35px;">Delivered By</div>
                    <div style="border-top: 1px dashed #666; display: inline-block; padding-top: 3px; font-weight: bold; font-size: 10px;">Job Worker's Signature</div>
                </td>
                <td style="width: 33.3%; padding: 10px 8px; vertical-align: top; border-right: 1px solid #ccc; text-align: center;">
                    <div style="font-size: 10px; color: #666; margin-bottom: 35px;">Quality Checked & Verified</div>
                    <div style="border-top: 1px dashed #666; display: inline-block; padding-top: 3px; font-weight: bold; font-size: 10px;">QC Inspector</div>
                </td>
                <td style="width: 33.4%; padding: 10px 8px; vertical-align: top; text-align: center;">
                    <div style="font-size: 10px; color: #666; margin-bottom: 35px;">Received & Stocked By</div>
                    <div style="border-top: 1px dashed #666; display: inline-block; padding-top: 3px; font-weight: bold; font-size: 10px;">Store Keeper Signature</div>
                </td>
            </tr>
        </table>
        </div>
    </div>

    <script>
        function shareWhatsApp() {{
            const text = encodeURIComponent(
                "*{meta['company_name']} - MATERIAL RECEIPT NOTE*\n" +
                "Receipt No: {vch_no}\n" +
                "Date: {date}\n" +
                "Received From: {party_name}\n" +
                "Into MC: {to_mc or 'MAIN STORE'}\n" +
                "Total Qty: {total_qty:,.2f} Pcs.\n" +
                "View Receipt: " + window.location.href
            );
            window.open("https://api.whatsapp.com/send?text=" + text, "_blank");
        }}
    </script>
</body>
</html>'''


def render_purchase_html(header, party, comp, p_cfg, items, sundries, transport, item_batches, meta, css, doc_title="PURCHASE VOUCHER"):
    vch_no = header.get('VchNo', '').strip()
    date = header.get('Date', '')
    series_name = header.get('SeriesName', '')
    supplier_name = party.get('PrintName') or party.get('Name') or header.get('PartyName') or 'Supplier / Vendor'
    supplier_addr = f"{party.get('Address1', '')} {party.get('Address2', '')}".strip()
    supplier_gstin = party.get('GSTNo') or 'Unregistered'

    item_rows, spacer_rows, total_qty, total_amt = build_inventory_table_rows(items, item_batches)

    # Sundries Rows
    sundry_rows = ""
    for s in sundries:
        s_name = s.get('SundryName', '')
        s_signed = s.get('Amount', 0) or 0  # signed by the sundry's nature: negative = subtractive
        s_amt = abs(s_signed)
        s_rate = s.get('Rate', 0) or 0
        s_base = s.get('BaseVal', 0) or 0
        if s_amt < 0.001:
            continue
        rate_label = f" ({s_rate:g}%)" if s_rate > 0 and s_base and abs(s_base * s_rate / 100 - s_amt) <= 1 else ""
        prefix = "-" if s_signed < 0 else ""
        sundry_rows += f'''
        <tr>
            <td style="padding: 2px 4px; font-size: 10.5px;">{s_name}{rate_label}:</td>
            <td style="padding: 2px 4px; font-size: 10.5px; text-align: right; font-weight: bold;">{prefix}&#x20B9;{s_amt:,.2f}</td>
        </tr>
        '''

    net_amt = header.get('VchAmt', 0) or total_amt

    return f'''<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{doc_title} - {vch_no}</title>
    <style>{css}</style>
</head>
<body>
    <div class="action-bar no-print">
        <button onclick="window.history.back();" class="btn btn-back">&#x2190; Back</button>
        <button onclick="window.print();" class="btn btn-print">&#x1F5B6; Print / Save PDF</button>
        <button onclick="shareWhatsApp();" class="btn btn-wa">&#x1F4AC; WhatsApp</button>
    </div>

    <div class="mobile-hint no-print">&#x1F4F1; Swipe left/right to view full voucher &bull; Pinch to zoom</div>
    <div class="voucher-scroll-wrap">
        <div class="voucher-box">
        <!-- TOP TITLE & COPY BAR -->
        <table style="width: 100%; border-collapse: collapse; border-bottom: 1.5px solid #000; font-size: 11px;">
            <tr>
                <td style="padding: 4px 8px; width: 33%; font-weight: bold;">{f'GSTIN: {meta["gstin"]}' if meta['gstin'] else ''}</td>
                <td style="padding: 4px 8px; width: 34%; text-align: center; font-size: 13px; font-weight: bold; letter-spacing: 1px;">{doc_title}</td>
                <td style="padding: 4px 8px; width: 33%; text-align: right; font-weight: bold;">(BUYER'S AUDIT COPY)</td>
            </tr>
        </table>

        <!-- COMPANY HEADER -->
        <div style="text-align: center; padding: 8px 12px; border-bottom: 1.5px solid #000;">
            <div style="font-size: 22px; font-weight: bold; letter-spacing: 0.5px;">{meta['company_name']}</div>
            {f'<div style="font-size: 11px; font-weight: bold; margin-top: 2px;">{meta["addr1"]}</div>' if meta['addr1'] else ''}
            {f'<div style="font-size: 11px;">{meta["addr2"]}</div>' if meta['addr2'] else ''}
            <div style="font-size: 11px; margin-top: 3px;">
                {f'Mobile: <strong>{meta["mobile"]}</strong> | ' if meta['mobile'] else ''}State: <strong>TAMIL NADU (Code: 33)</strong>
            </div>
            <div style="font-size: 9.5px; color: #555; margin-top: 2px;">(Inward Supply Verification & Tax Invoice Entry)</div>
        </div>

        <!-- SUPPLIER & PURCHASE DETAILS -->
        <table style="width: 100%; border-collapse: collapse; border-bottom: 1.5px solid #000; font-size: 11px;">
            <tr>
                <!-- Left: Supplier -->
                <td style="width: 55%; border-right: 1.5px solid #000; padding: 8px 10px; vertical-align: top;">
                    <div style="font-size: 10px; font-weight: bold; text-transform: uppercase; color: #1e3a8a; margin-bottom: 4px; letter-spacing: 0.5px;">
                        SUPPLIER / VENDOR (விற்பனையாளர் விபரம்):
                    </div>
                    <div style="font-size: 13px; font-weight: bold; color: #000;">
                        {supplier_name}
                    </div>
                    {f'<div style="color: #475569; font-size: 10px; margin-top: 2px;">{supplier_addr}</div>' if supplier_addr else ''}
                    <div style="color: #475569; font-size: 10px; margin-top: 2px;">GSTIN: <strong>{supplier_gstin}</strong></div>
                    <div style="margin-top: 6px; padding: 4px 6px; background: #f1f5f9; border-radius: 4px; font-size: 10px;">
                        <strong>Delivered To (Buyer):</strong> {meta['company_name']}
                    </div>
                </td>

                <!-- Right: Purchase Details -->
                <td style="width: 45%; padding: 8px 10px; vertical-align: top;">
                    <table style="width: 100%; border-collapse: collapse; font-size: 11px; line-height: 1.6;">
                        <tr>
                            <td style="width: 45%; font-weight: bold;">Purchase Vch No:</td>
                            <td style="font-weight: bold; font-size: 13px; color: #1e3a8a;">{vch_no}</td>
                        </tr>
                        <tr>
                            <td style="font-weight: bold;">Purchase Date:</td>
                            <td style="font-weight: bold;">{date}</td>
                        </tr>
                        {f'<tr><td style="font-weight: bold;">Series:</td><td style="font-weight: bold;">{series_name}</td></tr>' if series_name else ''}
                        <tr>
                            <td style="font-weight: bold;">Destination Store:</td>
                            <td>{header.get('from_mc') or header.get('to_mc') or 'MAIN STORE'}</td>
                        </tr>
                        <tr>
                            <td style="font-weight: bold;">Transport / LR:</td>
                            <td>{transport.get('Transport') or '-'}</td>
                        </tr>
                    </table>
                </td>
            </tr>
        </table>

        <!-- ITEMS GRID -->
        <table class="tbl-bordered" style="width: 100%; border-collapse: collapse; border-bottom: 1.5px solid #000;">
            <thead>
                <tr style="background: #f8fafc; border-bottom: 1.5px solid #000; font-size: 11px; font-weight: bold;">
                    <th class="tbl-bordered" style="padding: 5px; width: 5%; text-align: center;">S.N.</th>
                    <th class="tbl-bordered" style="padding: 5px 8px; text-align: left;">Description of Goods Purchased</th>
                    <th class="tbl-bordered" style="padding: 5px 4px; width: 12%; text-align: center;">HSN/SAC</th>
                    <th class="tbl-bordered" style="padding: 5px 8px; width: 14%; text-align: right;">Quantity</th>
                    <th class="tbl-bordered" style="padding: 5px 4px; width: 8%; text-align: center;">Unit</th>
                    <th class="tbl-bordered" style="padding: 5px 8px; width: 12%; text-align: right;">Rate (&#x20B9;)</th>
                    <th class="tbl-bordered" style="padding: 5px 8px; width: 15%; text-align: right;">Amount (&#x20B9;)</th>
                </tr>
            </thead>
            <tbody>
                {item_rows}
                {spacer_rows}
            </tbody>
            <tfoot>
                <tr style="font-weight: bold; background: #f8fafc; font-size: 11px; border-top: 1.5px solid #000;">
                    <td class="tbl-bordered" colspan="3" style="padding: 6px 8px; text-align: right;">SUB TOTAL:</td>
                    <td class="tbl-bordered" style="padding: 6px 8px; text-align: right; font-weight: bold;">{total_qty:,.2f}</td>
                    <td class="tbl-bordered" style="padding: 6px 4px; text-align: center;">Pcs.</td>
                    <td class="tbl-bordered" style="padding: 6px 8px; text-align: right;">-</td>
                    <td class="tbl-bordered" style="padding: 6px 8px; text-align: right; font-weight: bold;">&#x20B9;{total_amt:,.2f}</td>
                </tr>
            </tfoot>
        </table>

        <!-- TOTALS & SUNDRIES SECTION -->
        <table style="width: 100%; border-collapse: collapse; border-bottom: 1.5px solid #000; font-size: 11px;">
            <tr>
                <td style="width: 58%; padding: 8px 10px; vertical-align: top; border-right: 1px solid #000;">
                    <strong>Net Bill Amount (in words):</strong><br>
                    <span style="font-size: 12px; font-weight: bold; color: #1e3a8a;">{header.get('AmountInWords', '')}</span>
                </td>
                <td style="width: 42%; padding: 6px 10px; vertical-align: top;">
                    <table style="width: 100%; border-collapse: collapse; font-size: 11px;">
                        {sundry_rows}
                        <tr style="border-top: 1px solid #000; font-size: 12px; font-weight: bold;">
                            <td style="padding: 4px 4px;">NET TOTAL:</td>
                            <td style="padding: 4px 4px; text-align: right; color: #1e3a8a;">&#x20B9;{net_amt:,.2f}</td>
                        </tr>
                    </table>
                </td>
            </tr>
        </table>

        <!-- 3 SIGNATURE BLOCKS -->
        <table style="width: 100%; font-size: 11px;">
            <tr>
                <td style="width: 33.3%; padding: 10px 8px; vertical-align: top; border-right: 1px solid #ccc; text-align: center;">
                    <div style="font-size: 10px; color: #666; margin-bottom: 35px;">Goods Received In Store</div>
                    <div style="border-top: 1px dashed #666; display: inline-block; padding-top: 3px; font-weight: bold; font-size: 10px;">Storekeeper Signature</div>
                </td>
                <td style="width: 33.3%; padding: 10px 8px; vertical-align: top; border-right: 1px solid #ccc; text-align: center;">
                    <div style="font-size: 10px; color: #666; margin-bottom: 35px;">Material Inspected (QC)</div>
                    <div style="border-top: 1px dashed #666; display: inline-block; padding-top: 3px; font-weight: bold; font-size: 10px;">QC Incharge</div>
                </td>
                <td style="width: 33.4%; padding: 10px 8px; vertical-align: top; text-align: center;">
                    <div style="font-size: 10px; color: #666; margin-bottom: 35px;">Passed & Entered In Books</div>
                    <div style="border-top: 1px dashed #666; display: inline-block; padding-top: 3px; font-weight: bold; font-size: 10px;">Accounts Approved</div>
                </td>
            </tr>
        </table>
        </div>
    </div>

    <script>
        function shareWhatsApp() {{
            const text = encodeURIComponent(
                "*{meta['company_name']} - {doc_title}*\n" +
                "Voucher No: {vch_no}\n" +
                "Date: {date}\n" +
                "Supplier: {supplier_name}\n" +
                "Total Amount: Rs. {net_amt:,.2f}\n" +
                "View: " + window.location.href
            );
            window.open("https://api.whatsapp.com/send?text=" + text, "_blank");
        }}
    </script>
</body>
</html>'''


def render_delivery_challan_html(header, party, comp, p_cfg, items, transport, item_batches, linked_sales, meta, css):
    vch_no = header.get('VchNo', '').strip()
    date = header.get('Date', '')
    party_name = party.get('PrintName') or party.get('Name') or header.get('PartyName') or 'Consignee'
    party_addr = f"{party.get('Address1', '')} {party.get('Address2', '')}".strip()
    party_gstin = party.get('GSTNo') or 'Unregistered'

    item_rows, spacer_rows, total_qty, total_amt = build_inventory_table_rows(items, item_batches)

    linked_inv_html = ""
    if linked_sales and len(linked_sales) > 0:
        linked_inv_html = f'''
        <tr>
            <td style="font-weight: bold; color: #15803d;">Tax Invoice Ref:</td>
            <td style="font-weight: bold; color: #15803d;">{linked_sales[0].get('VchNo', '').strip()} ({linked_sales[0].get('Date', '')})</td>
        </tr>
        '''

    return f'''<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>DELIVERY CHALLAN - {vch_no}</title>
    <style>{css}</style>
</head>
<body>
    <div class="action-bar no-print">
        <button onclick="window.history.back();" class="btn btn-back">&#x2190; Back</button>
        <button onclick="window.print();" class="btn btn-print">&#x1F5B6; Print / Save PDF</button>
        <button onclick="shareWhatsApp();" class="btn btn-wa">&#x1F4AC; WhatsApp</button>
    </div>

    <div class="mobile-hint no-print">&#x1F4F1; Swipe left/right to view full challan &bull; Pinch to zoom</div>
    <div class="voucher-scroll-wrap">
        <div class="voucher-box">
        <!-- TOP TITLE & COPY BAR -->
        <table style="width: 100%; border-collapse: collapse; border-bottom: 1.5px solid #000; font-size: 11px;">
            <tr>
                <td style="padding: 4px 8px; width: 33%; font-weight: bold;">{f'GSTIN: {meta["gstin"]}' if meta['gstin'] else ''}</td>
                <td style="padding: 4px 8px; width: 34%; text-align: center; font-size: 13px; font-weight: bold; letter-spacing: 1px;">DELIVERY CHALLAN</td>
                <td style="padding: 4px 8px; width: 33%; text-align: right; font-weight: bold;">(ORIGINAL FOR CONSIGNEE)</td>
            </tr>
        </table>

        <!-- COMPANY HEADER -->
        <div style="text-align: center; padding: 8px 12px; border-bottom: 1.5px solid #000;">
            <div style="font-size: 22px; font-weight: bold; letter-spacing: 0.5px;">{meta['company_name']}</div>
            {f'<div style="font-size: 11px; font-weight: bold; margin-top: 2px;">{meta["addr1"]}</div>' if meta['addr1'] else ''}
            {f'<div style="font-size: 11px;">{meta["addr2"]}</div>' if meta['addr2'] else ''}
            <div style="font-size: 11px; margin-top: 3px;">
                {f'Mobile: <strong>{meta["mobile"]}</strong> | ' if meta['mobile'] else ''}State: <strong>TAMIL NADU (Code: 33)</strong>
            </div>
            <div style="font-size: 9.5px; color: #555; margin-top: 2px;">(Issued Under Rule 55 of CGST Rules, 2017 - Transportation Other Than By Way of Supply)</div>
        </div>

        <!-- CONSIGNEE & DISPATCH DETAILS -->
        <table style="width: 100%; border-collapse: collapse; border-bottom: 1.5px solid #000; font-size: 11px;">
            <tr>
                <!-- Left: Consignee -->
                <td style="width: 55%; border-right: 1.5px solid #000; padding: 8px 10px; vertical-align: top;">
                    <div style="font-size: 10px; font-weight: bold; text-transform: uppercase; color: #1e3a8a; margin-bottom: 4px; letter-spacing: 0.5px;">
                        CONSIGNEE (DELIVER TO / பெறுபவர் விபரம்):
                    </div>
                    <div style="font-size: 13px; font-weight: bold; color: #000;">
                        {party_name}
                    </div>
                    {f'<div style="color: #475569; font-size: 10px; margin-top: 2px;">{party_addr}</div>' if party_addr else ''}
                    <div style="color: #475569; font-size: 10px; margin-top: 2px;">GSTIN: <strong>{party_gstin}</strong></div>
                </td>

                <!-- Right: Dispatch -->
                <td style="width: 45%; padding: 8px 10px; vertical-align: top;">
                    <table style="width: 100%; border-collapse: collapse; font-size: 11px; line-height: 1.6;">
                        <tr>
                            <td style="width: 45%; font-weight: bold;">Challan No.:</td>
                            <td style="font-weight: bold; font-size: 13px; color: #1e3a8a;">{vch_no}</td>
                        </tr>
                        <tr>
                            <td style="font-weight: bold;">Challan Date:</td>
                            <td style="font-weight: bold;">{date}</td>
                        </tr>
                        {linked_inv_html}
                        <tr>
                            <td style="font-weight: bold;">Transport:</td>
                            <td>{transport.get('Transport') or '-'}</td>
                        </tr>
                        <tr>
                            <td style="font-weight: bold;">Vehicle No.:</td>
                            <td>{transport.get('VehicleNo') or '-'}</td>
                        </tr>
                        <tr>
                            <td style="font-weight: bold;">LR / GR No.:</td>
                            <td>{transport.get('GrNo') or '-'}</td>
                        </tr>
                    </table>
                </td>
            </tr>
        </table>

        <!-- ITEMS GRID -->
        <table class="tbl-bordered" style="width: 100%; border-collapse: collapse; border-bottom: 1.5px solid #000;">
            <thead>
                <tr style="background: #f8fafc; border-bottom: 1.5px solid #000; font-size: 11px; font-weight: bold;">
                    <th class="tbl-bordered" style="padding: 5px; width: 5%; text-align: center;">S.N.</th>
                    <th class="tbl-bordered" style="padding: 5px 8px; text-align: left;">Description of Goods Dispatched</th>
                    <th class="tbl-bordered" style="padding: 5px 4px; width: 12%; text-align: center;">HSN/SAC</th>
                    <th class="tbl-bordered" style="padding: 5px 8px; width: 14%; text-align: right;">Quantity</th>
                    <th class="tbl-bordered" style="padding: 5px 4px; width: 8%; text-align: center;">Unit</th>
                    <th class="tbl-bordered" style="padding: 5px 8px; width: 12%; text-align: right;">Rate (&#x20B9;)</th>
                    <th class="tbl-bordered" style="padding: 5px 8px; width: 15%; text-align: right;">Declared Val (&#x20B9;)</th>
                </tr>
            </thead>
            <tbody>
                {item_rows}
                {spacer_rows}
            </tbody>
            <tfoot>
                <tr style="font-weight: bold; background: #f8fafc; font-size: 11px; border-top: 1.5px solid #000;">
                    <td class="tbl-bordered" colspan="3" style="padding: 6px 8px; text-align: right;">TOTAL DISPATCHED:</td>
                    <td class="tbl-bordered" style="padding: 6px 8px; text-align: right; font-weight: bold;">{total_qty:,.2f}</td>
                    <td class="tbl-bordered" style="padding: 6px 4px; text-align: center;">Pcs.</td>
                    <td class="tbl-bordered" style="padding: 6px 8px; text-align: right;">-</td>
                    <td class="tbl-bordered" style="padding: 6px 8px; text-align: right; font-weight: bold;">&#x20B9;{total_amt:,.2f}</td>
                </tr>
            </tfoot>
        </table>

        <div style="padding: 6px 10px; font-size: 10px; color: #555; border-bottom: 1.5px solid #000; line-height: 1.4;">
            <strong>Declaration:</strong> Goods dispatched under Rule 55 of CGST Rules for Transportation / Delivery. Not for commercial sale unless covered by attached Tax Invoice.
        </div>

        <!-- 3 SIGNATURE BLOCKS -->
        <table style="width: 100%; font-size: 11px;">
            <tr>
                <td style="width: 33.3%; padding: 10px 8px; vertical-align: top; border-right: 1px solid #ccc; text-align: center;">
                    <div style="font-size: 10px; color: #666; margin-bottom: 35px;">Prepared By</div>
                    <div style="border-top: 1px dashed #666; display: inline-block; padding-top: 3px; font-weight: bold; font-size: 10px;">Dispatch Executive</div>
                </td>
                <td style="width: 33.3%; padding: 10px 8px; vertical-align: top; border-right: 1px solid #ccc; text-align: center;">
                    <div style="font-size: 10px; color: #666; margin-bottom: 35px;">Transporter / Driver</div>
                    <div style="border-top: 1px dashed #666; display: inline-block; padding-top: 3px; font-weight: bold; font-size: 10px;">Carrier's Signature</div>
                </td>
                <td style="width: 33.4%; padding: 10px 8px; vertical-align: top; text-align: center;">
                    <div style="font-size: 10px; color: #666; margin-bottom: 35px;">Consignee Acknowledgment</div>
                    <div style="border-top: 1px dashed #666; display: inline-block; padding-top: 3px; font-weight: bold; font-size: 10px;">Received in Good Condition</div>
                </td>
            </tr>
        </table>
        </div>
    </div>

    <script>
        function shareWhatsApp() {{
            const text = encodeURIComponent(
                "*{meta['company_name']} - DELIVERY CHALLAN*\n" +
                "Challan No: {vch_no}\n" +
                "Date: {date}\n" +
                "Consignee: {party_name}\n" +
                "Total Qty: {total_qty:,.2f} Pcs.\n" +
                "View Challan: " + window.location.href
            );
            window.open("https://api.whatsapp.com/send?text=" + text, "_blank");
        }}
    </script>
</body>
</html>'''


def render_order_html(header, party, comp, p_cfg, items, sundries, transport, item_batches, meta, css):
    vch_no = header.get('VchNo', '').strip()
    date = header.get('Date', '')
    vch_type = header.get('VchType', 12)
    is_sales_order = (vch_type == 12)

    order_title = "SALES ORDER" if is_sales_order else "PURCHASE ORDER"
    copy_type = "ORDER CONFIRMATION" if is_sales_order else "PROCUREMENT COPY"
    party_box_title = "CUSTOMER DETAILS (ஆர்டர் விபரம்):" if is_sales_order else "SUPPLIER / VENDOR (ஆர்டர் விபரம்):"

    party_name = party.get('PrintName') or party.get('Name') or header.get('PartyName') or 'Party'
    party_addr = f"{party.get('Address1', '')} {party.get('Address2', '')}".strip()
    party_gstin = party.get('GSTNo') or 'Unregistered'

    item_rows, spacer_rows, total_qty, total_amt = build_inventory_table_rows(items, item_batches)
    net_amt = header.get('VchAmt', 0) or total_amt

    return f'''<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{order_title} - {vch_no}</title>
    <style>{css}</style>
</head>
<body>
    <div class="action-bar no-print">
        <button onclick="window.history.back();" class="btn btn-back">&#x2190; Back</button>
        <button onclick="window.print();" class="btn btn-print">&#x1F5B6; Print / Save PDF</button>
        <button onclick="shareWhatsApp();" class="btn btn-wa">&#x1F4AC; WhatsApp</button>
    </div>

    <div class="mobile-hint no-print">&#x1F4F1; Swipe left/right to view full order &bull; Pinch to zoom</div>
    <div class="voucher-scroll-wrap">
        <div class="voucher-box">
        <!-- TOP TITLE & COPY BAR -->
        <table style="width: 100%; border-collapse: collapse; border-bottom: 1.5px solid #000; font-size: 11px;">
            <tr>
                <td style="padding: 4px 8px; width: 33%; font-weight: bold;">{f'GSTIN: {meta["gstin"]}' if meta['gstin'] else ''}</td>
                <td style="padding: 4px 8px; width: 34%; text-align: center; font-size: 13px; font-weight: bold; letter-spacing: 1px;">{order_title}</td>
                <td style="padding: 4px 8px; width: 33%; text-align: right; font-weight: bold;">({copy_type})</td>
            </tr>
        </table>

        <!-- COMPANY HEADER -->
        <div style="text-align: center; padding: 8px 12px; border-bottom: 1.5px solid #000;">
            <div style="font-size: 22px; font-weight: bold; letter-spacing: 0.5px;">{meta['company_name']}</div>
            {f'<div style="font-size: 11px; font-weight: bold; margin-top: 2px;">{meta["addr1"]}</div>' if meta['addr1'] else ''}
            {f'<div style="font-size: 11px;">{meta["addr2"]}</div>' if meta['addr2'] else ''}
            <div style="font-size: 11px; margin-top: 3px;">
                {f'Mobile: <strong>{meta["mobile"]}</strong> | ' if meta['mobile'] else ''}State: <strong>TAMIL NADU (Code: 33)</strong>
            </div>
        </div>

        <!-- PARTY & ORDER DETAILS -->
        <table style="width: 100%; border-collapse: collapse; border-bottom: 1.5px solid #000; font-size: 11px;">
            <tr>
                <!-- Left: Party -->
                <td style="width: 55%; border-right: 1.5px solid #000; padding: 8px 10px; vertical-align: top;">
                    <div style="font-size: 10px; font-weight: bold; text-transform: uppercase; color: #1e3a8a; margin-bottom: 4px; letter-spacing: 0.5px;">
                        {party_box_title}
                    </div>
                    <div style="font-size: 13px; font-weight: bold; color: #000;">
                        {party_name}
                    </div>
                    {f'<div style="color: #475569; font-size: 10px; margin-top: 2px;">{party_addr}</div>' if party_addr else ''}
                    <div style="color: #475569; font-size: 10px; margin-top: 2px;">GSTIN: <strong>{party_gstin}</strong></div>
                </td>

                <!-- Right: Order Meta -->
                <td style="width: 45%; padding: 8px 10px; vertical-align: top;">
                    <table style="width: 100%; border-collapse: collapse; font-size: 11px; line-height: 1.6;">
                        <tr>
                            <td style="width: 45%; font-weight: bold;">Order No.:</td>
                            <td style="font-weight: bold; font-size: 13px; color: #1e3a8a;">{vch_no}</td>
                        </tr>
                        <tr>
                            <td style="font-weight: bold;">Order Date:</td>
                            <td style="font-weight: bold;">{date}</td>
                        </tr>
                        <tr>
                            <td style="font-weight: bold;">Delivery Due:</td>
                            <td>{transport.get('GrDate') or 'Immediate'}</td>
                        </tr>
                        <tr>
                            <td style="font-weight: bold;">Station:</td>
                            <td>{transport.get('Station') or '-'}</td>
                        </tr>
                    </table>
                </td>
            </tr>
        </table>

        <!-- ITEMS GRID -->
        <table class="tbl-bordered" style="width: 100%; border-collapse: collapse; border-bottom: 1.5px solid #000;">
            <thead>
                <tr style="background: #f8fafc; border-bottom: 1.5px solid #000; font-size: 11px; font-weight: bold;">
                    <th class="tbl-bordered" style="padding: 5px; width: 5%; text-align: center;">S.N.</th>
                    <th class="tbl-bordered" style="padding: 5px 8px; text-align: left;">Item Description / Specifications</th>
                    <th class="tbl-bordered" style="padding: 5px 4px; width: 12%; text-align: center;">HSN/SAC</th>
                    <th class="tbl-bordered" style="padding: 5px 8px; width: 14%; text-align: right;">Order Qty</th>
                    <th class="tbl-bordered" style="padding: 5px 4px; width: 8%; text-align: center;">Unit</th>
                    <th class="tbl-bordered" style="padding: 5px 8px; width: 12%; text-align: right;">Agreed Rate (&#x20B9;)</th>
                    <th class="tbl-bordered" style="padding: 5px 8px; width: 15%; text-align: right;">Amount (&#x20B9;)</th>
                </tr>
            </thead>
            <tbody>
                {item_rows}
                {spacer_rows}
            </tbody>
            <tfoot>
                <tr style="font-weight: bold; background: #f8fafc; font-size: 11px; border-top: 1.5px solid #000;">
                    <td class="tbl-bordered" colspan="3" style="padding: 6px 8px; text-align: right;">TOTAL ORDER VALUE:</td>
                    <td class="tbl-bordered" style="padding: 6px 8px; text-align: right; font-weight: bold;">{total_qty:,.2f}</td>
                    <td class="tbl-bordered" style="padding: 6px 4px; text-align: center;">Pcs.</td>
                    <td class="tbl-bordered" style="padding: 6px 8px; text-align: right;">-</td>
                    <td class="tbl-bordered" style="padding: 6px 8px; text-align: right; font-weight: bold;">&#x20B9;{net_amt:,.2f}</td>
                </tr>
            </tfoot>
        </table>

        <div style="padding: 6px 10px; font-size: 11px; border-bottom: 1.5px solid #000; background: #fafafa;">
            <strong>Order Amount (in words):</strong> <span style="font-weight: bold; color: #1e3a8a;">{header.get('AmountInWords', '')}</span>
        </div>

        <!-- 2 SIGNATURE BLOCKS -->
        <table style="width: 100%; font-size: 11px;">
            <tr>
                <td style="width: 50%; padding: 12px 10px; vertical-align: top; border-right: 1px solid #ccc; text-align: center;">
                    <div style="font-size: 10px; color: #666; margin-bottom: 35px;">Confirmed & Accepted By</div>
                    <div style="border-top: 1px dashed #666; display: inline-block; padding-top: 3px; font-weight: bold; font-size: 10px;">Party's Authorized Signature</div>
                </td>
                <td style="width: 50%; padding: 12px 10px; vertical-align: top; text-align: center;">
                    <div style="font-weight: bold; font-size: 10px; margin-bottom: 35px;">{meta['sign_for']}</div>
                    <div style="border-top: 1px dashed #666; display: inline-block; padding-top: 3px; font-weight: bold; font-size: 10px;">{meta['sign_title']}</div>
                </td>
            </tr>
        </table>
        </div>
    </div>

    <script>
        function shareWhatsApp() {{
            const text = encodeURIComponent(
                "*{meta['company_name']} - {order_title}*\n" +
                "Order No: {vch_no}\n" +
                "Date: {date}\n" +
                "Party: {party_name}\n" +
                "Total Amount: Rs. {net_amt:,.2f}\n" +
                "View Order: " + window.location.href
            );
            window.open("https://api.whatsapp.com/send?text=" + text, "_blank");
        }}
    </script>
</body>
</html>'''


def render_note_html(header, party, comp, p_cfg, items, sundries, transport, item_batches, meta, css, is_credit=True):
    vch_no = header.get('VchNo', '').strip()
    date = header.get('Date', '')
    title = "CREDIT NOTE / SALES RETURN" if is_credit else "DEBIT NOTE / PURCHASE RETURN"
    box_title = "CUSTOMER (CREDITED TO):" if is_credit else "SUPPLIER (DEBITED TO):"
    copy_type = "ORIGINAL FOR RECIPIENT"

    party_name = party.get('PrintName') or party.get('Name') or header.get('PartyName') or 'Party'
    party_addr = f"{party.get('Address1', '')} {party.get('Address2', '')}".strip()
    party_gstin = party.get('GSTNo') or 'Unregistered'

    item_rows, spacer_rows, total_qty, total_amt = build_inventory_table_rows(items, item_batches)
    net_amt = header.get('VchAmt', 0) or total_amt

    return f'''<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{title} - {vch_no}</title>
    <style>{css}</style>
</head>
<body>
    <div class="action-bar no-print">
        <button onclick="window.history.back();" class="btn btn-back">&#x2190; Back</button>
        <button onclick="window.print();" class="btn btn-print">&#x1F5B6; Print / Save PDF</button>
        <button onclick="shareWhatsApp();" class="btn btn-wa">&#x1F4AC; WhatsApp</button>
    </div>

    <div class="mobile-hint no-print">&#x1F4F1; Swipe left/right to view full note &bull; Pinch to zoom</div>
    <div class="voucher-scroll-wrap">
        <div class="voucher-box">
        <!-- TOP TITLE & COPY BAR -->
        <table style="width: 100%; border-collapse: collapse; border-bottom: 1.5px solid #000; font-size: 11px;">
            <tr>
                <td style="padding: 4px 8px; width: 33%; font-weight: bold;">{f'GSTIN: {meta["gstin"]}' if meta['gstin'] else ''}</td>
                <td style="padding: 4px 8px; width: 34%; text-align: center; font-size: 13px; font-weight: bold; letter-spacing: 1px;">{title}</td>
                <td style="padding: 4px 8px; width: 33%; text-align: right; font-weight: bold;">({copy_type})</td>
            </tr>
        </table>

        <!-- COMPANY HEADER -->
        <div style="text-align: center; padding: 8px 12px; border-bottom: 1.5px solid #000;">
            <div style="font-size: 22px; font-weight: bold; letter-spacing: 0.5px;">{meta['company_name']}</div>
            {f'<div style="font-size: 11px; font-weight: bold; margin-top: 2px;">{meta["addr1"]}</div>' if meta['addr1'] else ''}
            {f'<div style="font-size: 11px;">{meta["addr2"]}</div>' if meta['addr2'] else ''}
            <div style="font-size: 11px; margin-top: 3px;">
                {f'Mobile: <strong>{meta["mobile"]}</strong> | ' if meta['mobile'] else ''}State: <strong>TAMIL NADU (Code: 33)</strong>
            </div>
            <div style="font-size: 9.5px; color: #555; margin-top: 2px;">(Issued Under Section 34 of the CGST Act, 2017)</div>
        </div>

        <!-- PARTY & NOTE DETAILS -->
        <table style="width: 100%; border-collapse: collapse; border-bottom: 1.5px solid #000; font-size: 11px;">
            <tr>
                <!-- Left: Party -->
                <td style="width: 55%; border-right: 1.5px solid #000; padding: 8px 10px; vertical-align: top;">
                    <div style="font-size: 10px; font-weight: bold; text-transform: uppercase; color: #b91c1c; margin-bottom: 4px; letter-spacing: 0.5px;">
                        {box_title}
                    </div>
                    <div style="font-size: 13px; font-weight: bold; color: #000;">
                        {party_name}
                    </div>
                    {f'<div style="color: #475569; font-size: 10px; margin-top: 2px;">{party_addr}</div>' if party_addr else ''}
                    <div style="color: #475569; font-size: 10px; margin-top: 2px;">GSTIN: <strong>{party_gstin}</strong></div>
                </td>

                <!-- Right: Meta -->
                <td style="width: 45%; padding: 8px 10px; vertical-align: top;">
                    <table style="width: 100%; border-collapse: collapse; font-size: 11px; line-height: 1.6;">
                        <tr>
                            <td style="width: 45%; font-weight: bold;">Note No.:</td>
                            <td style="font-weight: bold; font-size: 13px; color: #b91c1c;">{vch_no}</td>
                        </tr>
                        <tr>
                            <td style="font-weight: bold;">Date:</td>
                            <td style="font-weight: bold;">{date}</td>
                        </tr>
                        <tr>
                            <td style="font-weight: bold;">Original Bill Ref:</td>
                            <td>{header.get('VchNo', '').strip() or 'As per ledger'}</td>
                        </tr>
                        <tr>
                            <td style="font-weight: bold;">Reason:</td>
                            <td>Goods Return / Quality Adjustment</td>
                        </tr>
                    </table>
                </td>
            </tr>
        </table>

        <!-- ITEMS GRID -->
        <table class="tbl-bordered" style="width: 100%; border-collapse: collapse; border-bottom: 1.5px solid #000;">
            <thead>
                <tr style="background: #f8fafc; border-bottom: 1.5px solid #000; font-size: 11px; font-weight: bold;">
                    <th class="tbl-bordered" style="padding: 5px; width: 5%; text-align: center;">S.N.</th>
                    <th class="tbl-bordered" style="padding: 5px 8px; text-align: left;">Description of Goods Returned / Adjusted</th>
                    <th class="tbl-bordered" style="padding: 5px 4px; width: 12%; text-align: center;">HSN/SAC</th>
                    <th class="tbl-bordered" style="padding: 5px 8px; width: 14%; text-align: right;">Quantity</th>
                    <th class="tbl-bordered" style="padding: 5px 4px; width: 8%; text-align: center;">Unit</th>
                    <th class="tbl-bordered" style="padding: 5px 8px; width: 12%; text-align: right;">Rate (&#x20B9;)</th>
                    <th class="tbl-bordered" style="padding: 5px 8px; width: 15%; text-align: right;">Amount (&#x20B9;)</th>
                </tr>
            </thead>
            <tbody>
                {item_rows}
                {spacer_rows}
            </tbody>
            <tfoot>
                <tr style="font-weight: bold; background: #f8fafc; font-size: 11px; border-top: 1.5px solid #000;">
                    <td class="tbl-bordered" colspan="3" style="padding: 6px 8px; text-align: right;">TOTAL ADJUSTMENT:</td>
                    <td class="tbl-bordered" style="padding: 6px 8px; text-align: right; font-weight: bold;">{total_qty:,.2f}</td>
                    <td class="tbl-bordered" style="padding: 6px 4px; text-align: center;">Pcs.</td>
                    <td class="tbl-bordered" style="padding: 6px 8px; text-align: right;">-</td>
                    <td class="tbl-bordered" style="padding: 6px 8px; text-align: right; font-weight: bold;">&#x20B9;{net_amt:,.2f}</td>
                </tr>
            </tfoot>
        </table>

        <div style="padding: 6px 10px; font-size: 11px; border-bottom: 1.5px solid #000; background: #fafafa;">
            <strong>Adjustment Amount (in words):</strong> <span style="font-weight: bold; color: #b91c1c;">{header.get('AmountInWords', '')}</span>
        </div>

        <!-- 2 SIGNATURE BLOCKS -->
        <table style="width: 100%; font-size: 11px;">
            <tr>
                <td style="width: 50%; padding: 12px 10px; vertical-align: top; border-right: 1px solid #ccc; text-align: center;">
                    <div style="font-size: 10px; color: #666; margin-bottom: 35px;">Prepared & Checked By</div>
                    <div style="border-top: 1px dashed #666; display: inline-block; padding-top: 3px; font-weight: bold; font-size: 10px;">Accounts Executive</div>
                </td>
                <td style="width: 50%; padding: 12px 10px; vertical-align: top; text-align: center;">
                    <div style="font-weight: bold; font-size: 10px; margin-bottom: 35px;">{meta['sign_for']}</div>
                    <div style="border-top: 1px dashed #666; display: inline-block; padding-top: 3px; font-weight: bold; font-size: 10px;">{meta['sign_title']}</div>
                </td>
            </tr>
        </table>
        </div>
    </div>

    <script>
        function shareWhatsApp() {{
            const text = encodeURIComponent(
                "*{meta['company_name']} - {title}*\n" +
                "Note No: {vch_no}\n" +
                "Date: {date}\n" +
                "Party: {party_name}\n" +
                "Amount: Rs. {net_amt:,.2f}\n" +
                "View Note: " + window.location.href
            );
            window.open("https://api.whatsapp.com/send?text=" + text, "_blank");
        }}
    </script>
</body>
</html>'''

