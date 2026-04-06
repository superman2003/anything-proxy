/* Anything Proxy - Accounts Management */

function accountsApp() {
    return {
        activeTab: 'accounts',
        accounts: [],
        stats: { total_accounts: 0, active_accounts: 0, error_accounts: 0, total_requests: 0 },
        filterStatus: '',
        searchKeyword: '',

        // Modals
        showAddModal: false,
        showEditModal: false,
        showImportModal: false,
        editingId: null,

        // Form
        form: { name: '', access_token: '', refresh_token: '', project_group_id: '', proxy_url: '', note: '' },
        formError: '',
        formLoading: false,

        // Import
        importText: '',
        importResult: null,
        importLoading: false,

        // Bulk
        bulkLoading: false,
        selectedAccountIds: [],

        // Outlook tab
        outlookAccounts: [],
        outlookStats: { total: 0, pending: 0, linked: 0, error: 0 },
        outlookBulkLoading: false,
        showOutlookImportModal: false,
        outlookImportText: '',
        outlookImportResult: null,
        outlookImportLoading: false,
        showOutlookEditModal: false,
        outlookEditingId: null,
        outlookForm: { email: '', password: '', client_id: '', ms_refresh_token: '' },
        outlookFormError: '',
        outlookFormLoading: false,

        // Config tab
        healthResult: null,
        configApiKeys: [],

        // API Keys tab
        apiKeys: [],
        newKeyName: '',
        newlyCreatedKey: '',
        creatingKey: false,
        defaultModel: 'claude-opus-4-6',
        modelOptions: ['claude-opus-4-6', 'claude-sonnet-4-6', 'gpt-5.4'],
        pricingCatalog: [],

        // Toast
        toast: { show: false, message: '', type: 'success' },

        // Usage tab
        usageStats: { totals: {}, by_model: [], by_status: [], by_key: [], daily: [], pricing_catalog: [] },
        usageDays: '7',
        usageLogs: [],
        usageLogPage: 1,
        usageLogPages: 1,
        usageLogTotal: 0,
        usageLogFilter: { model: '', status: '' },

        async init() {
            await this.loadAccounts();
            await this.loadStats();
            await this.loadApiKeys();
        },

        async loadAccounts() {
            const params = new URLSearchParams();
            if (this.filterStatus) params.set('status', this.filterStatus);
            if (this.searchKeyword) params.set('keyword', this.searchKeyword);
            const resp = await apiFetch('/admin/api/accounts?' + params.toString());
            if (!resp) return;
            const data = await resp.json();
            this.accounts = data.accounts || [];
            this.syncSelectedAccounts();
            if (data.stats) {
                this.stats.total_accounts = data.stats.total;
                this.stats.active_accounts = data.stats.active;
                this.stats.error_accounts = data.stats.error;
            }
        },

        syncSelectedAccounts() {
            const visibleIds = new Set(this.accounts.map(acc => acc.id));
            this.selectedAccountIds = this.selectedAccountIds.filter(id => visibleIds.has(id));
        },

        isAccountSelected(id) {
            return this.selectedAccountIds.includes(id);
        },

        allVisibleAccountsSelected() {
            return this.accounts.length > 0 && this.accounts.every(acc => this.selectedAccountIds.includes(acc.id));
        },

        toggleAccountSelection(id, checked) {
            if (checked) {
                if (!this.selectedAccountIds.includes(id)) {
                    this.selectedAccountIds.push(id);
                }
                return;
            }
            this.selectedAccountIds = this.selectedAccountIds.filter(selectedId => selectedId !== id);
        },

        toggleAllAccountSelections(checked) {
            this.selectedAccountIds = checked ? this.accounts.map(acc => acc.id) : [];
        },

        clearAccountSelections() {
            this.selectedAccountIds = [];
        },

        async loadStats() {
            const resp = await apiFetch('/admin/api/stats/overview');
            if (!resp) return;
            const data = await resp.json();
            Object.assign(this.stats, data);
        },

        statusClass(acc) {
            if (!acc.is_active) return 'bg-gray-100 text-gray-600';
            switch (acc.status) {
                case 'active': return 'bg-green-100 text-green-700';
                case 'error': return 'bg-red-100 text-red-700';
                case 'token_expired': return 'bg-yellow-100 text-yellow-700';
                case 'banned': return 'bg-red-100 text-red-700';
                default: return 'bg-gray-100 text-gray-600';
            }
        },

        statusText(acc) {
            if (!acc.is_active) return '已禁用';
            const map = { active: '活跃', error: '错误', token_expired: 'Token过期', banned: '封禁', disabled: '已禁用' };
            return map[acc.status] || acc.status;
        },

        async checkHealth() {
            try {
                const resp = await fetch('/health');
                this.healthResult = await resp.json();
            } catch (e) {
                this.healthResult = { status: 'error', active_accounts: 0 };
            }
        },

        formatBalance(val) {
            if (val === null || val === undefined) return '-';
            const n = Number(val);
            if (n === 0) return '0';
            // Format large numbers with commas for readability
            if (n >= 1e9) return (n / 1e9).toFixed(1) + 'B';
            if (n >= 1e6) return (n / 1e6).toFixed(1) + 'M';
            if (n >= 1e3) return (n / 1e3).toFixed(1) + 'K';
            return n.toLocaleString();
        },

        formatTime(t) {
            if (!t) return '-';
            try {
                const d = new Date(t);
                const now = new Date();
                const diff = (now - d) / 1000;
                if (diff < 60) return '刚刚';
                if (diff < 3600) return Math.floor(diff / 60) + '分钟前';
                if (diff < 86400) return Math.floor(diff / 3600) + '小时前';
                return d.toLocaleDateString('zh-CN') + ' ' + d.toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' });
            } catch { return t; }
        },

        editAccount(acc) {
            this.editingId = acc.id;
            // Need to fetch full account data (with unmasked tokens)
            this.formLoading = true;
            apiFetch('/admin/api/accounts/' + acc.id).then(async resp => {
                if (!resp) return;
                const data = await resp.json();
                this.form = {
                    name: data.name || '',
                    access_token: data.access_token || '',
                    refresh_token: data.refresh_token || '',
                    project_group_id: data.project_group_id || '',
                    proxy_url: data.proxy_url || '',
                    note: data.note || '',
                };
                this.showEditModal = true;
                this.formLoading = false;
            });
        },

        async saveAccount() {
            this.formError = '';
            if (!this.form.access_token.trim() || !this.form.project_group_id.trim()) {
                this.formError = 'Access Token 和 Project Group ID 为必填项';
                return;
            }
            this.formLoading = true;
            try {
                let resp;
                if (this.showEditModal && this.editingId) {
                    resp = await apiFetch('/admin/api/accounts/' + this.editingId, {
                        method: 'PUT',
                        body: JSON.stringify(this.form),
                    });
                } else {
                    resp = await apiFetch('/admin/api/accounts', {
                        method: 'POST',
                        body: JSON.stringify(this.form),
                    });
                }
                if (!resp) return;
                const data = await resp.json();
                if (resp.ok && data.success) {
                    this.showToast(this.showEditModal ? '账号更新成功' : '账号添加成功');
                    this.closeModals();
                    await this.loadAccounts();
                    await this.loadStats();
                } else {
                    this.formError = data.detail || '操作失败';
                }
            } catch (e) {
                this.formError = '网络错误: ' + e.message;
            } finally {
                this.formLoading = false;
            }
        },

        closeModals() {
            this.showAddModal = false;
            this.showEditModal = false;
            this.editingId = null;
            this.formError = '';
            this.form = { name: '', access_token: '', refresh_token: '', project_group_id: '', proxy_url: '', note: '' };
        },

        async deleteAccount(id, name) {
            if (!confirm(`确定要删除账号 "${name || id}" 吗？`)) return;
            const resp = await apiFetch('/admin/api/accounts/' + id, { method: 'DELETE' });
            if (!resp) return;
            if (resp.ok) {
                this.toggleAccountSelection(id, false);
                this.showToast('账号已删除');
                await this.loadAccounts();
                await this.loadStats();
            }
        },

        async deleteSelectedAccounts() {
            const ids = [...this.selectedAccountIds];
            if (!ids.length) {
                this.showToast('请先选择要删除的账号', 'error');
                return;
            }
            if (!confirm(`确定要批量删除选中的 ${ids.length} 个账号吗？`)) return;
            this.bulkLoading = true;
            try {
                const resp = await apiFetch('/admin/api/accounts/batch-delete', {
                    method: 'POST',
                    body: JSON.stringify({ ids }),
                });
                if (!resp) return;
                const data = await resp.json();
                if (resp.ok && data.success) {
                    this.clearAccountSelections();
                    const missing = (data.missing_ids || []).length;
                    this.showToast(
                        missing
                            ? `批量删除完成: 删除 ${data.deleted} 个，跳过 ${missing} 个`
                            : `批量删除完成: 删除 ${data.deleted} 个账号`
                    );
                    await this.loadAccounts();
                    await this.loadStats();
                } else {
                    this.showToast(data.detail || '批量删除失败', 'error');
                }
            } catch (e) {
                this.showToast('批量删除失败: ' + e.message, 'error');
            } finally {
                this.bulkLoading = false;
            }
        },

        async checkAccount(id) {
            const acc = this.accounts.find(a => a.id === id);
            if (acc) acc._checking = true;
            const resp = await apiFetch('/admin/api/accounts/' + id + '/check', { method: 'POST' });
            if (acc) acc._checking = false;
            if (!resp) return;
            const data = await resp.json();
            this.showToast(data.success ? '账号状态正常' : '检测失败: ' + (data.error || '未知错误'), data.success ? 'success' : 'error');
            await this.loadAccounts();
            await this.loadStats();
        },

        async refreshAccount(id) {
            const acc = this.accounts.find(a => a.id === id);
            if (acc) acc._refreshing = true;
            const resp = await apiFetch('/admin/api/accounts/' + id + '/refresh', { method: 'POST' });
            if (acc) acc._refreshing = false;
            if (!resp) return;
            const data = await resp.json();
            this.showToast(data.success ? 'Token刷新成功' : 'Token刷新失败', data.success ? 'success' : 'error');
            await this.loadAccounts();
        },

        async toggleAccount(id, currentState) {
            const action = currentState ? '禁用' : '启用';
            if (!confirm(`确定要${action}此账号吗？`)) return;
            const resp = await apiFetch('/admin/api/accounts/' + id + '/toggle', { method: 'POST' });
            if (!resp) return;
            const data = await resp.json();
            this.showToast(`账号已${data.is_active ? '启用' : '禁用'}`);
            await this.loadAccounts();
            await this.loadStats();
        },

        async refreshAll() {
            if (!confirm('确定要刷新所有账号的Token吗？')) return;
            this.bulkLoading = true;
            const resp = await apiFetch('/admin/api/accounts/refresh-all', { method: 'POST' });
            this.bulkLoading = false;
            if (!resp) return;
            const data = await resp.json();
            this.showToast(`刷新完成: 成功 ${data.success}, 失败 ${data.failed}`);
            await this.loadAccounts();
        },

        async checkAll() {
            if (!confirm('确定要检测所有账号状态吗？')) return;
            this.bulkLoading = true;
            const resp = await apiFetch('/admin/api/accounts/check-all', { method: 'POST' });
            this.bulkLoading = false;
            if (!resp) return;
            const data = await resp.json();
            const results = data.results || [];
            const ok = results.filter(r => r.status === 'active').length;
            const fail = results.length - ok;
            this.showToast(`检测完成: 正常 ${ok}, 异常 ${fail}`);
            await this.loadAccounts();
            await this.loadStats();
        },

        async doImport() {
            this.importResult = null;
            let accounts;
            try {
                accounts = JSON.parse(this.importText);
                if (!Array.isArray(accounts)) throw new Error('必须是JSON数组');
            } catch (e) {
                this.importResult = { imported: 0, errors: ['JSON格式错误: ' + e.message] };
                return;
            }
            this.importLoading = true;
            const resp = await apiFetch('/admin/api/accounts/batch-import', {
                method: 'POST',
                body: JSON.stringify({ accounts }),
            });
            this.importLoading = false;
            if (!resp) return;
            this.importResult = await resp.json();
            await this.loadAccounts();
            await this.loadStats();
        },

        showToast(message, type = 'success') {
            this.toast = { show: true, message, type };
            setTimeout(() => { this.toast.show = false; }, 3000);
        },

        // ─── Balance methods ─────────────────────────────────────

        async checkBalance(id) {
            const acc = this.accounts.find(a => a.id === id);
            if (acc) acc._checkingBalance = true;
            const resp = await apiFetch('/admin/api/accounts/' + id + '/balance', { method: 'POST' });
            if (acc) acc._checkingBalance = false;
            if (!resp) return;
            const data = await resp.json();
            if (data.success) {
                this.showToast('额度: ' + this.formatBalance(data.credit_balance));
            } else {
                this.showToast('查询失败: ' + (data.error || '未知错误'), 'error');
            }
            await this.loadAccounts();
        },

        async checkAllBalances() {
            if (!confirm('确定要查询所有账号的额度吗？')) return;
            this.bulkLoading = true;
            const resp = await apiFetch('/admin/api/accounts/check-balance-all', { method: 'POST' });
            this.bulkLoading = false;
            if (!resp) return;
            const data = await resp.json();
            const results = data.results || [];
            const ok = results.filter(r => !r.error).length;
            const fail = results.length - ok;
            this.showToast(`额度查询完成: 成功 ${ok}, 失败 ${fail}`);
            await this.loadAccounts();
        },

        // ─── API Keys methods ────────────────────────────────────

        async loadApiKeys() {
            const resp = await apiFetch('/admin/api/keys');
            if (!resp) return;
            const data = await resp.json();
            this.apiKeys = data.keys || [];
            this.configApiKeys = this.apiKeys.filter(k => k.is_active);
        },

        maskKey(key) {
            if (!key || key.length < 20) return key || '';
            return key.substring(0, 16) + '...' + key.substring(key.length - 4);
        },

        async createApiKey() {
            this.creatingKey = true;
            this.newlyCreatedKey = '';
            try {
                const resp = await apiFetch('/admin/api/keys', {
                    method: 'POST',
                    body: JSON.stringify({ name: this.newKeyName }),
                });
                if (!resp) return;
                const data = await resp.json();
                this.newlyCreatedKey = data.key;
                this.newKeyName = '';
                this.showToast('API 密钥已生成');
                await this.loadApiKeys();
            } catch (e) {
                this.showToast('生成失败: ' + e.message, 'error');
            } finally {
                this.creatingKey = false;
            }
        },

        async deleteApiKey(id, name) {
            if (!confirm(`确定要删除密钥 "${name || id}" 吗？`)) return;
            const resp = await apiFetch('/admin/api/keys/' + id, { method: 'DELETE' });
            if (!resp) return;
            if (resp.ok) {
                this.showToast('密钥已删除');
                await this.loadApiKeys();
            }
        },

        // ─── Config export ───────────────────────────────────────

        _buildConfig() {
            const baseUrl = window.location.origin;
            const apiKey = this.configApiKeys.length > 0 ? this.configApiKeys[0].key : null;
            const config = {
                api_base_url: baseUrl + '/v1',
                models: [...this.modelOptions],
                default_model: this.defaultModel,
            };
            if (apiKey) config.api_key = apiKey;
            config.env = {
                ANTHROPIC_BASE_URL: baseUrl,
            };
            if (apiKey) config.env.ANTHROPIC_API_KEY = apiKey;
            return config;
        },

        exportConfig() {
            const config = this._buildConfig();
            const blob = new Blob([JSON.stringify(config, null, 2)], { type: 'application/json' });
            const url = URL.createObjectURL(blob);
            const a = document.createElement('a');
            a.href = url;
            a.download = 'anything-proxy-config.json';
            a.click();
            URL.revokeObjectURL(url);
            this.showToast('配置已导出');
        },

        copyConfig() {
            const config = this._buildConfig();
            navigator.clipboard.writeText(JSON.stringify(config, null, 2));
            this.showToast('配置已复制到剪贴板');
        },

        // ─── Outlook tab methods ────────────────────────────────

        async loadOutlookAccounts() {
            const resp = await apiFetch('/admin/api/outlook-accounts');
            if (!resp) return;
            const data = await resp.json();
            this.outlookAccounts = data.accounts || [];
            if (data.stats) {
                Object.assign(this.outlookStats, data.stats);
            }
        },

        async doOutlookImport() {
            this.outlookImportResult = null;
            const text = this.outlookImportText.trim();
            if (!text) {
                this.outlookImportResult = { imported: 0, errors: ['请输入导入内容'] };
                return;
            }
            this.outlookImportLoading = true;
            try {
                const resp = await apiFetch('/admin/api/outlook-accounts/import', {
                    method: 'POST',
                    body: JSON.stringify({ text }),
                });
                if (!resp) return;
                this.outlookImportResult = await resp.json();
                await this.loadOutlookAccounts();
            } catch (e) {
                this.outlookImportResult = { imported: 0, errors: ['网络错误: ' + e.message] };
            } finally {
                this.outlookImportLoading = false;
            }
        },

        async loginOutlookAccount(id) {
            const acc = this.outlookAccounts.find(a => a.id === id);
            if (acc) acc._logging = true;
            this.showToast('正在自动登录，请等待...', 'success');
            try {
                const resp = await apiFetch('/admin/api/outlook-accounts/' + id + '/login', { method: 'POST' });
                if (!resp) return;
                const data = await resp.json();
                if (data.success) {
                    this.showToast('自动登录成功: ' + (data.email || ''));
                    await this.loadOutlookAccounts();
                    await this.loadAccounts();
                    await this.loadStats();
                } else {
                    this.showToast('自动登录失败: ' + (data.error || '未知错误'), 'error');
                    await this.loadOutlookAccounts();
                }
            } catch (e) {
                this.showToast('请求失败: ' + e.message, 'error');
            } finally {
                if (acc) acc._logging = false;
            }
        },

        async reloginOutlookAccount(id) {
            const acc = this.outlookAccounts.find(a => a.id === id);
            if (acc) acc._logging = true;
            this.showToast('正在重新登录，请等待...', 'success');
            try {
                const resp = await apiFetch('/admin/api/outlook-accounts/' + id + '/relogin', { method: 'POST' });
                if (!resp) return;
                const data = await resp.json();
                if (data.success) {
                    this.showToast('重新登录成功: ' + (data.email || ''));
                    await this.loadOutlookAccounts();
                    await this.loadAccounts();
                    await this.loadStats();
                } else {
                    this.showToast('重新登录失败: ' + (data.error || '未知错误'), 'error');
                    await this.loadOutlookAccounts();
                }
            } catch (e) {
                this.showToast('请求失败: ' + e.message, 'error');
            } finally {
                if (acc) acc._logging = false;
            }
        },

        async loginAllOutlook() {
            if (!confirm('确定要一键登录所有待处理/失败的邮箱吗？')) return;
            this.outlookBulkLoading = true;
            try {
                const resp = await apiFetch('/admin/api/outlook-accounts/login-all', { method: 'POST' });
                if (!resp) return;
                const data = await resp.json();
                this.showToast(`登录完成: 成功 ${data.success}, 失败 ${data.failed}`);
                await this.loadOutlookAccounts();
                await this.loadAccounts();
                await this.loadStats();
            } catch (e) {
                this.showToast('请求失败: ' + e.message, 'error');
            } finally {
                this.outlookBulkLoading = false;
            }
        },

        async deleteOutlookAccount(id, email) {
            if (!confirm(`确定要删除 Outlook 邮箱 "${email}" 吗？`)) return;
            const resp = await apiFetch('/admin/api/outlook-accounts/' + id, { method: 'DELETE' });
            if (!resp) return;
            if (resp.ok) {
                this.showToast('Outlook 邮箱已删除');
                await this.loadOutlookAccounts();
            }
        },

        editOutlookAccount(acc) {
            this.outlookEditingId = acc.id;
            this.outlookFormLoading = true;
            apiFetch('/admin/api/outlook-accounts/' + acc.id).then(async resp => {
                if (!resp) { this.outlookFormLoading = false; return; }
                const data = await resp.json();
                this.outlookForm = {
                    email: data.email || '',
                    password: data.password || '',
                    client_id: data.client_id || '',
                    ms_refresh_token: data.ms_refresh_token || '',
                };
                this.showOutlookEditModal = true;
                this.outlookFormLoading = false;
            });
        },

        async saveOutlookAccount() {
            this.outlookFormError = '';
            if (!this.outlookForm.email.trim()) {
                this.outlookFormError = '邮箱为必填项';
                return;
            }
            this.outlookFormLoading = true;
            try {
                const resp = await apiFetch('/admin/api/outlook-accounts/' + this.outlookEditingId, {
                    method: 'PUT',
                    body: JSON.stringify(this.outlookForm),
                });
                if (!resp) return;
                const data = await resp.json();
                if (resp.ok && data.success) {
                    this.showToast('Outlook 邮箱更新成功');
                    this.closeOutlookModals();
                    await this.loadOutlookAccounts();
                } else {
                    this.outlookFormError = data.detail || '操作失败';
                }
            } catch (e) {
                this.outlookFormError = '网络错误: ' + e.message;
            } finally {
                this.outlookFormLoading = false;
            }
        },

        closeOutlookModals() {
            this.showOutlookEditModal = false;
            this.outlookEditingId = null;
            this.outlookFormError = '';
            this.outlookForm = { email: '', password: '', client_id: '', ms_refresh_token: '' };
        },

        outlookStatusClass(status) {
            switch (status) {
                case 'linked': return 'bg-green-100 text-green-700';
                case 'pending': return 'bg-yellow-100 text-yellow-700';
                case 'error': return 'bg-red-100 text-red-700';
                default: return 'bg-gray-100 text-gray-600';
            }
        },

        outlookStatusText(status) {
            const map = { pending: '待处理', linked: '已关联', error: '失败' };
            return map[status] || status;
        },

        // ─── Usage tab methods ───────────────────────────────

        formatNumber(n) {
            if (n === null || n === undefined) return '0';
            n = Number(n);
            if (n >= 1e6) return (n / 1e6).toFixed(1) + 'M';
            if (n >= 1e3) return (n / 1e3).toFixed(1) + 'K';
            return n.toLocaleString();
        },

        formatUsd(value) {
            const amount = Number(value || 0);
            if (!amount) return '$0.00';
            if (amount < 0.01) return '$' + amount.toFixed(4);
            return '$' + amount.toFixed(2);
        },

        async loadUsageStats() {
            const resp = await apiFetch('/admin/api/usage/stats?days=' + this.usageDays);
            if (!resp) return;
            this.usageStats = await resp.json();
            this.pricingCatalog = this.usageStats.pricing_catalog || [];
            await this.loadUsageLogs();
        },

        async loadUsageLogs() {
            const params = new URLSearchParams();
            params.set('page', this.usageLogPage);
            params.set('page_size', '30');
            if (this.usageLogFilter.model) params.set('model', this.usageLogFilter.model);
            if (this.usageLogFilter.status) params.set('status', this.usageLogFilter.status);
            const resp = await apiFetch('/admin/api/usage/logs?' + params.toString());
            if (!resp) return;
            const data = await resp.json();
            this.usageLogs = data.logs || [];
            this.usageLogTotal = data.total || 0;
            this.usageLogPages = data.total_pages || 1;
        },

        async clearUsageLogs() {
            const resp = await apiFetch('/admin/api/usage/logs', { method: 'DELETE' });
            if (!resp) return;
            this.showToast('使用日志已清除');
            await this.loadUsageStats();
        },

        // ─── Common ─────────────────────────────────────────────

        async logout() {
            await apiFetch('/admin/logout', { method: 'POST' });
            window.location.href = '/admin/login';
        }
    };
}
