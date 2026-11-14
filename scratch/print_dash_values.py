import urllib.request
import json

dash_url = 'http://localhost:3000/api/dashboard?company=COMP0044&fy=2026&fromDate=2026-04-01&toDate=2026-10-20'
res = urllib.request.urlopen(dash_url)
data = json.loads(res.read().decode('utf-8'))

print("Sales:", data['sales'])
print("Purchases:", data['purchases'])
print("Receipts:", data['receipts'])
print("Payments:", data['payments'])
print("Receivables:", data['receivables'])
print("Payables:", data['payables'])

