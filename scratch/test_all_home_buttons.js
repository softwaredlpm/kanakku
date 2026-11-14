const { spawn } = require('child_process');
const http = require('http');

const chromePath = 'C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe';

const chrome = spawn(chromePath, [
    '--headless',
    '--disable-gpu',
    '--remote-debugging-port=9228',
    '--user-data-dir=C:\\Users\\SERVER\\AppData\\Local\\Temp\\chrome_home_all_buttons'
]);

setTimeout(async () => {
    try {
        http.get('http://127.0.0.1:9228/json/list', (res) => {
            let data = '';
            res.on('data', chunk => data += chunk);
            res.on('end', async () => {
                const tabs = JSON.parse(data);
                const wsUrl = tabs[0].webSocketDebuggerUrl;
                
                const ws = new WebSocket(wsUrl);
                ws.onopen = () => {
                    ws.send(JSON.stringify({ id: 1, method: 'Runtime.enable' }));
                    ws.send(JSON.stringify({ id: 2, method: 'Page.navigate', params: { url: 'http://localhost:3000' } }));
                };

                let errors = [];

                ws.onmessage = (evt) => {
                    const msg = JSON.parse(evt.data);
                    if (msg.method === 'Runtime.consoleAPICalled' && msg.params.type === 'error') {
                        errors.push(msg.params.args.map(a => a.value || a.description));
                    }
                    if (msg.method === 'Runtime.exceptionThrown') {
                        errors.push(msg.params.exceptionDetails.text);
                    }
                    if (msg.id === 2) {
                        setTimeout(() => {
                            ws.send(JSON.stringify({
                                id: 20,
                                method: 'Runtime.evaluate',
                                params: {
                                    expression: `(() => {
                                        const results = {};
                                        
                                        // 1. Sales Return (Type 3)
                                        openCreateVoucherModal(3);
                                        results.salesReturnTitle = document.getElementById('createVchModalTitle')?.innerText;
                                        closeCreateVoucherModal();

                                        // 2. Order (Type 12)
                                        openCreateVoucherModal(12);
                                        results.orderTitle = document.getElementById('createVchModalTitle')?.innerText;
                                        closeCreateVoucherModal();

                                        // 3. Payment (Type 19)
                                        openCreateVoucherModal(19);
                                        results.paymentTitle = document.getElementById('createVchModalTitle')?.innerText;
                                        closeCreateVoucherModal();

                                        // 4. View & Share Invoice
                                        switchTab('vouchers');
                                        results.tabVouchersActive = !document.getElementById('tab-vouchers').classList.contains('hidden');

                                        // 5. Daily Summary
                                        openReportView('daily-summary');
                                        results.daybookViewActive = !document.getElementById('view-daybook').classList.contains('hidden');
                                        closeReportView();

                                        // 6. Reports -> Sales
                                        openReportView('sales');
                                        results.salesViewActive = !document.getElementById('tab-vouchers').classList.contains('hidden');

                                        // 7. Bottom Navigation
                                        switchTab('masters');
                                        results.tabMastersActive = !document.getElementById('tab-masters').classList.contains('hidden');
                                        switchTab('reports');
                                        results.tabReportsActive = !document.getElementById('tab-reports').classList.contains('hidden');
                                        switchTab('more');
                                        results.tabMoreActive = !document.getElementById('tab-more').classList.contains('hidden');
                                        switchTab('home');
                                        results.tabHomeActive = !document.getElementById('tab-home').classList.contains('hidden');

                                        return results;
                                    })()`,
                                    returnByValue: true
                                }
                            }));
                        }, 2500);
                    }
                    if (msg.id === 20) {
                        console.log('ALL HOME BUTTONS VERIFICATION RESULTS:');
                        console.log(JSON.stringify(msg.result.result.value, null, 2));
                        console.log('Errors caught during full run:', errors);
                        ws.close();
                        chrome.kill();
                        process.exit(0);
                    }
                };
            });
        }).on('error', (err) => {
            console.error('HTTP get error:', err);
            chrome.kill();
        });
    } catch (e) {
        console.error('Catch error:', e);
        chrome.kill();
    }
}, 1500);

