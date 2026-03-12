/**
 * iFlow2API 管理后台 JavaScript
 */

// API 基础路径
const API_BASE = '/admin';

// 全局状态
const state = {
    token: localStorage.getItem('admin_token'),
    currentUser: null,
    ws: null,
    settings: {},
    refreshInterval: null,
};

// ==================== 工具函数 ====================

/**
 * 显示 Toast 通知
 */
function showToast(message, type = 'info') {
    const container = document.getElementById('toast-container');
    const toast = document.createElement('div');
    toast.className = `toast ${type}`;
    toast.textContent = message;
    container.appendChild(toast);

    setTimeout(() => {
        toast.style.animation = 'slideIn 0.3s ease reverse';
        setTimeout(() => toast.remove(), 300);
    }, 3000);
}

/**
 * 发送 API 请求
 */
async function apiRequest(endpoint, options = {}) {
    const url = `${API_BASE}${endpoint}`;
    const headers = {
        'Content-Type': 'application/json',
        ...options.headers,
    };

    if (state.token) {
        headers['Authorization'] = `Bearer ${state.token}`;
    }

    try {
        const response = await fetch(url, {
            ...options,
            headers,
        });

        const data = await response.json();

        if (!response.ok) {
            throw new Error(data.detail || '请求失败');
        }

        return data;
    } catch (error) {
        console.error('API Error:', error);
        throw error;
    }
}

/**
 * 格式化时间
 */
function formatUptime(seconds) {
    const hours = Math.floor(seconds / 3600);
    const minutes = Math.floor((seconds % 3600) / 60);
    const secs = Math.floor(seconds % 60);
    return `${hours.toString().padStart(2, '0')}:${minutes.toString().padStart(2, '0')}:${secs.toString().padStart(2, '0')}`;
}

/**
 * 格式化日期时间
 */
function formatDateTime(isoString) {
    if (!isoString) return '--';
    const date = new Date(isoString);
    return date.toLocaleString('zh-CN');
}

function escapeHtml(value) {
    return String(value ?? '')
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;')
        .replace(/'/g, '&#39;');
}

function formatAuthType(authType) {
    const authTypeMap = {
        'api-key': 'API Key',
        'oauth-iflow': 'OAuth',
        'cookie': 'Cookie',
        'not_logged_in': '未登录',
    };
    return authTypeMap[authType] || authType || '未知';
}

function renderUpstreamAccounts(accounts, tableId, includeActions = false) {
    const table = document.getElementById(tableId);
    if (!table) return;

    const tbody = table.querySelector('tbody');
    if (!tbody) return;

    tbody.innerHTML = '';

    if (!accounts || accounts.length === 0) {
        const tr = document.createElement('tr');
        tr.innerHTML = `<td colspan="${includeActions ? 5 : 4}">暂无账号</td>`;
        tbody.appendChild(tr);
        return;
    }

    accounts.forEach((account) => {
        const tr = document.createElement('tr');
        const statusText = account.enabled ? '启用' : '停用';
        const detailText = [account.email, account.phone, account.cookie_expires_at]
            .filter(Boolean)
            .join(' / ');
        const labelText = account.is_primary
            ? `${escapeHtml(account.label)} <span class="hint">(主账号)</span>`
            : escapeHtml(account.label);

        if (includeActions) {
            tr.innerHTML = `
                <td>${labelText}</td>
                <td>${escapeHtml(formatAuthType(account.auth_type))}</td>
                <td>${escapeHtml(account.api_key_masked || '--')}<br><span class="hint">${escapeHtml(account.base_url || '')}</span></td>
                <td>${escapeHtml(statusText)}<br><span class="hint">${escapeHtml(detailText || '--')}</span></td>
                <td>
                    <button class="btn btn-secondary btn-sm" onclick="activateUpstreamAccount('${account.id}')" ${account.is_primary ? 'disabled' : ''}>设为主账号</button>
                    <button class="btn btn-secondary btn-sm" onclick="toggleUpstreamAccount('${account.id}', ${!account.enabled})">${account.enabled ? '停用' : '启用'}</button>
                    <button class="btn btn-danger btn-sm" onclick="deleteUpstreamAccount('${account.id}')">删除</button>
                </td>
            `;
        } else {
            tr.innerHTML = `
                <td>${labelText}</td>
                <td>${escapeHtml(formatAuthType(account.auth_type))}</td>
                <td>${escapeHtml(account.api_key_masked || '--')}</td>
                <td>${escapeHtml(statusText)}</td>
            `;
        }
        tbody.appendChild(tr);
    });
}

// ==================== 认证相关 ====================

/**
 * 检查登录状态
 */
async function checkAuth() {
    if (!state.token) {
        showLoginPage();
        return false;
    }

    try {
        // 获取状态来验证 token
        await apiRequest('/status');
        showMainPage();
        return true;
    } catch (error) {
        localStorage.removeItem('admin_token');
        state.token = null;
        showLoginPage();
        return false;
    }
}

/**
 * 检查是否需要初始化
 */
async function checkSetup() {
    try {
        const data = await apiRequest('/check-setup');
        const hint = document.getElementById('login-hint');
        if (data.needs_setup) {
            hint.textContent = '首次使用，请设置管理员账户';
        } else {
            hint.textContent = '';
        }
    } catch (error) {
        console.error('Check setup error:', error);
    }
}

/**
 * 登录
 */
async function login(username, password) {
    try {
        const data = await apiRequest('/login', {
            method: 'POST',
            body: JSON.stringify({ username, password }),
        });

        state.token = data.token;
        localStorage.setItem('admin_token', data.token);
        state.currentUser = username;

        showToast(data.message, 'success');
        showMainPage();
    } catch (error) {
        showToast(error.message, 'error');
    }
}

/**
 * 登出
 */
async function logout() {
    try {
        await apiRequest('/logout', { method: 'POST' });
    } catch (error) {
        console.error('Logout error:', error);
    }

    localStorage.removeItem('admin_token');
    state.token = null;
    state.currentUser = null;

    if (state.ws) {
        state.ws.close();
        state.ws = null;
    }

    if (state.refreshInterval) {
        clearInterval(state.refreshInterval);
        state.refreshInterval = null;
    }

    showLoginPage();
    showToast('已退出登录', 'info');
}

// ==================== 页面切换 ====================

function showLoginPage() {
    document.getElementById('login-page').classList.add('active');
    document.getElementById('main-page').classList.remove('active');
    checkSetup();
}

function showMainPage() {
    document.getElementById('login-page').classList.remove('active');
    document.getElementById('main-page').classList.add('active');

    // 初始化数据
    loadStatus();
    loadAccountInfo();
    loadSettings();
    loadUsers();

    // 连接 WebSocket
    connectWebSocket();

    // 启动定时刷新
    startAutoRefresh();
}

function showSection(sectionId) {
    // 隐藏所有区块
    document.querySelectorAll('.section').forEach(section => {
        section.classList.remove('active');
    });

    // 显示目标区块
    document.getElementById(`${sectionId}-section`).classList.add('active');

    // 更新导航
    document.querySelectorAll('.nav-item').forEach(item => {
        item.classList.remove('active');
        if (item.dataset.page === sectionId) {
            item.classList.add('active');
        }
    });

    // 更新标题
    const titles = {
        dashboard: '仪表盘',
        settings: '设置',
        users: '用户管理',
        logs: '日志',
    };
    document.getElementById('page-title').textContent = titles[sectionId] || sectionId;
}

// ==================== 数据加载 ====================

/**
 * 加载账号信息
 */
async function loadAccountInfo() {
    try {
        const data = await apiRequest('/account-info');
        state.accountInfo = data;

        // 更新认证方式
        document.getElementById('account-auth-type').textContent = formatAuthType(data.auth_type);

        // 更新 API Key
        document.getElementById('account-api-key').textContent = data.api_key_masked || '未配置';

        // 更新邮箱和手机号（如果有）
        const emailRow = document.getElementById('account-email-row');
        const phoneRow = document.getElementById('account-phone-row');
        const cookieExpireRow = document.getElementById('account-cookie-expire-row');

        if (data.email) {
            document.getElementById('account-email').textContent = data.email;
            emailRow.style.display = '';
        } else {
            emailRow.style.display = 'none';
        }

        if (data.phone) {
            document.getElementById('account-phone').textContent = data.phone;
            phoneRow.style.display = '';
        } else {
            phoneRow.style.display = 'none';
        }

        if (data.cookie_expires_at) {
            document.getElementById('account-cookie-expire').textContent = data.cookie_expires_at;
            cookieExpireRow.style.display = '';
        } else {
            cookieExpireRow.style.display = 'none';
        }

        document.getElementById('account-total').textContent = data.total_accounts || 0;
        document.getElementById('account-enabled-total').textContent = data.enabled_accounts || 0;

        renderUpstreamAccounts(data.accounts || [], 'account-list-table', false);
        renderUpstreamAccounts(data.accounts || [], 'upstream-accounts-table', true);
    } catch (error) {
        console.error('Load account info error:', error);
    }
}

/**
 * 加载系统状态
 */
async function loadStatus() {
    try {
        const data = await apiRequest('/status');

        // 更新服务器状态
        const statusBadge = document.getElementById('server-status');
        statusBadge.className = `status-badge ${data.server.state}`;
        statusBadge.textContent = {
            stopped: '已停止',
            running: '运行中',
            error: '异常',
        }[data.server.state] || data.server.state;

        // 更新错误消息
        document.getElementById('server-error').textContent = data.server.error_message || '';

        // 更新运行时间
        document.getElementById('uptime').textContent = formatUptime(data.process.uptime);

        // 更新 WebSocket 连接数
        document.getElementById('ws-connections').textContent = data.connections.websocket_count;

        // 更新系统信息
        document.getElementById('system-platform').textContent = data.system.platform;
        document.getElementById('system-arch').textContent = data.system.architecture;
        document.getElementById('python-version').textContent = data.system.python_version.split(' ')[0];
        document.getElementById('start-time').textContent = formatDateTime(data.process.start_time);

    } catch (error) {
        console.error('Load status error:', error);
    }
}

/**
 * 加载设置
 */
async function loadSettings() {
    try {
        const data = await apiRequest('/settings');
        state.settings = data;

        // 账号池新增表单只保留默认 Base URL，不自动回填真实 API Key
        document.getElementById('setting-account-label').value = '';
        document.getElementById('setting-api-key').value = '';
        document.getElementById('setting-base-url').value = data.base_url || '';
        
        // 填充服务器配置
        document.getElementById('setting-host').value = data.host || '';
        document.getElementById('setting-port').value = data.port || 28000;

        // 填充界面设置
        document.getElementById('setting-theme').value = data.theme_mode || 'system';
        document.getElementById('setting-language').value = data.language || 'zh';
        
        // 填充内容处理设置
        document.getElementById('setting-preserve-reasoning').checked = data.preserve_reasoning_content || false;
        
        // 填充上游 API 设置
        document.getElementById('setting-api-concurrency').value = data.api_concurrency || 1;
        
        // 填充安全认证设置
        document.getElementById('setting-custom-api-key').value = data.custom_api_key || '';
        document.getElementById('setting-custom-auth-header').value = data.custom_auth_header || '';
        
        // 填充代理设置
        document.getElementById('setting-proxy-enabled').checked = data.upstream_proxy_enabled || false;
        document.getElementById('setting-proxy-url').value = data.upstream_proxy || '';

    } catch (error) {
        console.error('Load settings error:', error);
    }
}

/**
 * 保存设置
 */
async function saveSettings() {
    const settings = {
        // 当前主账号默认 Base URL
        base_url: document.getElementById('setting-base-url').value,
        // 服务器配置
        host: document.getElementById('setting-host').value,
        port: parseInt(document.getElementById('setting-port').value),
        // 界面设置
        theme_mode: document.getElementById('setting-theme').value,
        language: document.getElementById('setting-language').value,
        // 内容处理设置
        preserve_reasoning_content: document.getElementById('setting-preserve-reasoning').checked,
        // 上游 API 设置
        api_concurrency: parseInt(document.getElementById('setting-api-concurrency').value) || 1,
        // 安全认证设置
        custom_api_key: document.getElementById('setting-custom-api-key').value,
        custom_auth_header: document.getElementById('setting-custom-auth-header').value,
        // 代理设置
        upstream_proxy_enabled: document.getElementById('setting-proxy-enabled').checked,
        upstream_proxy: document.getElementById('setting-proxy-url').value,
    };

    try {
        await apiRequest('/settings', {
            method: 'PUT',
            body: JSON.stringify(settings),
        });
        showToast('设置已保存', 'success');
    } catch (error) {
        showToast(error.message, 'error');
    }
}

async function createApiKeyAccount() {
    const label = document.getElementById('setting-account-label').value.trim();
    const apiKey = document.getElementById('setting-api-key').value.trim();
    const baseUrl = document.getElementById('setting-base-url').value.trim();

    if (!apiKey) {
        showToast('请输入 API Key', 'error');
        return;
    }

    try {
        await apiRequest('/upstream-accounts', {
            method: 'POST',
            body: JSON.stringify({
                label,
                api_key: apiKey,
                base_url: baseUrl,
            }),
        });
        showToast('账号已添加到账号池', 'success');
        document.getElementById('setting-account-label').value = '';
        document.getElementById('setting-api-key').value = '';
        loadAccountInfo();
    } catch (error) {
        showToast(error.message, 'error');
    }
}

/**
 * Cookie 登录
 */
async function cookieLogin() {
    const cookieInput = document.getElementById('cookie-input');
    const cookieStatus = document.getElementById('cookie-status');
    const cookieSubmitBtn = document.getElementById('cookie-submit-btn');

    const cookie = cookieInput.value.trim();

    if (!cookie) {
        cookieStatus.textContent = '请输入 Cookie';
        cookieStatus.style.color = 'red';
        return;
    }

    if (!cookie.includes('BXAuth=')) {
        cookieStatus.textContent = 'Cookie 必须包含 BXAuth 字段';
        cookieStatus.style.color = 'red';
        return;
    }

    cookieStatus.textContent = '正在登录...';
    cookieStatus.style.color = 'blue';
    cookieSubmitBtn.disabled = true;

    try {
        const result = await apiRequest('/cookie/login', {
            method: 'POST',
            body: JSON.stringify({ cookie })
        });

        if (result.success) {
            showToast('Cookie 登录成功！', 'success');
            cookieInput.value = '';
            cookieStatus.textContent = '';
            closeCookieModal();
            loadSettings();
            loadAccountInfo();
        } else {
            cookieStatus.textContent = `登录失败: ${result.message}`;
            cookieStatus.style.color = 'red';
        }
    } catch (error) {
        cookieStatus.textContent = `登录失败: ${error.message}`;
        cookieStatus.style.color = 'red';
    } finally {
        cookieSubmitBtn.disabled = false;
    }
}

/**
 * 打开 Cookie 登录模态框
 */
function openCookieModal() {
    const modal = document.getElementById('cookie-modal');
    modal.classList.add('active');
}

/**
 * 关闭 Cookie 登录模态框
 */
function closeCookieModal() {
    const modal = document.getElementById('cookie-modal');
    modal.classList.remove('active');
    document.getElementById('cookie-input').value = '';
    document.getElementById('cookie-status').textContent = '';
}

/**
 * OAuth 登录
 */
let _oauthMessageHandler = null;

async function oauthLogin() {
    try {
        // 获取 OAuth URL
        const data = await apiRequest('/oauth/url');
        const authUrl = data.auth_url;
        
        // 打开新窗口进行 OAuth 登录
        const width = 600;
        const height = 700;
        const left = (window.innerWidth - width) / 2;
        const top = (window.innerHeight - height) / 2;
        
        const oauthWindow = window.open(
            authUrl,
            'iFlow OAuth',
            `width=${width},height=${height},left=${left},top=${top},toolbar=no,menubar=no`
        );
        
        // 移除之前的监听器（避免重复添加）
        if (_oauthMessageHandler) {
            window.removeEventListener('message', _oauthMessageHandler);
        }
        
        // 创建新的 OAuth 回调消息监听器
        _oauthMessageHandler = async (event) => {
            if (event.data && event.data.type === 'oauth_callback') {
                const code = event.data.code;
                if (code) {
                    try {
                        const result = await apiRequest('/oauth/callback', {
                            method: 'POST',
                            body: JSON.stringify({ code }),
                        });
                        showToast(result.message, 'success');
                        document.getElementById('setting-api-key').value = '';
                        loadSettings();
                        loadAccountInfo();
                    } catch (error) {
                        showToast(error.message, 'error');
                    }
                }
                // 处理完成后移除监听器
                window.removeEventListener('message', _oauthMessageHandler);
                _oauthMessageHandler = null;
            }
        };
        
        window.addEventListener('message', _oauthMessageHandler);
        
    } catch (error) {
        showToast(error.message, 'error');
    }
}

async function toggleUpstreamAccount(accountId, enabled) {
    try {
        await apiRequest(`/upstream-accounts/${accountId}/enabled`, {
            method: 'PATCH',
            body: JSON.stringify({ enabled }),
        });
        showToast('账号状态已更新', 'success');
        loadAccountInfo();
    } catch (error) {
        showToast(error.message, 'error');
    }
}

async function activateUpstreamAccount(accountId) {
    try {
        await apiRequest(`/upstream-accounts/${accountId}/activate`, {
            method: 'POST',
        });
        showToast('主账号已切换', 'success');
        loadSettings();
        loadAccountInfo();
    } catch (error) {
        showToast(error.message, 'error');
    }
}

async function deleteUpstreamAccount(accountId) {
    if (!confirm('确定要删除这个上游账号吗？')) {
        return;
    }

    try {
        await apiRequest(`/upstream-accounts/${accountId}`, {
            method: 'DELETE',
        });
        showToast('账号已删除', 'success');
        loadSettings();
        loadAccountInfo();
    } catch (error) {
        showToast(error.message, 'error');
    }
}

/**
 * 加载用户列表
 */
async function loadUsers() {
    try {
        const users = await apiRequest('/users');
        const tbody = document.querySelector('#users-table tbody');
        tbody.innerHTML = '';

        users.forEach(user => {
            const tr = document.createElement('tr');
            tr.innerHTML = `
                <td>${user.username}</td>
                <td>${formatDateTime(user.created_at)}</td>
                <td>${formatDateTime(user.last_login)}</td>
                <td>
                    <button class="btn btn-danger btn-sm" onclick="deleteUser('${user.username}')">删除</button>
                </td>
            `;
            tbody.appendChild(tr);
        });
    } catch (error) {
        console.error('Load users error:', error);
    }
}

/**
 * 添加用户
 */
async function addUser(username, password) {
    try {
        await apiRequest('/users', {
            method: 'POST',
            body: JSON.stringify({ username, password }),
        });
        showToast('用户已添加', 'success');
        loadUsers();
    } catch (error) {
        showToast(error.message, 'error');
    }
}

/**
 * 删除用户
 */
async function deleteUser(username) {
    if (!confirm(`确定要删除用户 "${username}" 吗？`)) {
        return;
    }

    try {
        await apiRequest(`/users/${username}`, { method: 'DELETE' });
        showToast('用户已删除', 'success');
        loadUsers();
    } catch (error) {
        showToast(error.message, 'error');
    }
}

/**
 * 修改密码
 */
async function changePassword(oldPassword, newPassword) {
    try {
        await apiRequest('/change-password', {
            method: 'POST',
            body: JSON.stringify({
                old_password: oldPassword,
                new_password: newPassword,
            }),
        });
        showToast('密码已修改', 'success');
    } catch (error) {
        showToast(error.message, 'error');
    }
}

/**
 * 加载日志
 */
async function loadLogs() {
    try {
        const data = await apiRequest('/logs?lines=200');
        const logContent = document.getElementById('log-content');
        logContent.textContent = data.logs.join('\n') || '暂无日志';
    } catch (error) {
        document.getElementById('log-content').textContent = '加载日志失败: ' + error.message;
    }
}

// ==================== WebSocket ====================

function connectWebSocket() {
    if (state.ws) {
        state.ws.close();
    }

    const wsProtocol = location.protocol === 'https:' ? 'wss:' : 'ws:';
    // 在 URL 中添加 token 查询参数（后端要求在握手阶段验证）
    const wsUrl = `${wsProtocol}//${location.host}${API_BASE}/ws?token=${encodeURIComponent(state.token)}`;

    state.ws = new WebSocket(wsUrl);

    state.ws.onopen = () => {
        console.log('WebSocket connected');
        // 连接已通过 URL 参数认证，无需再发送 auth 消息
    };

    state.ws.onmessage = (event) => {
        const data = JSON.parse(event.data);
        handleWebSocketMessage(data);
    };

    state.ws.onclose = () => {
        console.log('WebSocket disconnected');
        // 5秒后重连
        setTimeout(connectWebSocket, 5000);
    };

    state.ws.onerror = (error) => {
        console.error('WebSocket error:', error);
    };
}

function handleWebSocketMessage(data) {
    switch (data.type) {
        case 'status':
            // 更新状态
            break;
        case 'log':
            // 追加日志
            const logContent = document.getElementById('log-content');
            if (logContent && data.data) {
                logContent.textContent += `\n[${data.data.level}] ${data.data.message}`;
            }
            break;
        case 'settings_updated':
            showToast('设置已更新', 'info');
            loadSettings();
            break;
        case 'pong':
            // 心跳响应
            break;
    }
}

// ==================== 自动刷新 ====================

function startAutoRefresh() {
    if (state.refreshInterval) {
        clearInterval(state.refreshInterval);
    }

    state.refreshInterval = setInterval(() => {
        loadStatus();
    }, 5000);
}

// ==================== 事件绑定 ====================

document.addEventListener('DOMContentLoaded', () => {
    // 登录表单
    document.getElementById('login-form').addEventListener('submit', (e) => {
        e.preventDefault();
        const username = document.getElementById('username').value;
        const password = document.getElementById('password').value;
        login(username, password);
    });

    // 登出按钮
    document.getElementById('logout-btn').addEventListener('click', logout);

    // 导航切换
    document.querySelectorAll('.nav-item').forEach(item => {
        item.addEventListener('click', (e) => {
            e.preventDefault();
            const page = item.dataset.page;
            showSection(page);

            // 加载对应数据
            if (page === 'logs') {
                loadLogs();
            }
        });
    });

    // 设置保存
    document.getElementById('save-settings-btn').addEventListener('click', saveSettings);
    document.getElementById('reset-settings-btn').addEventListener('click', loadSettings);

    // iFlow 配置按钮
    document.getElementById('add-api-account-btn').addEventListener('click', createApiKeyAccount);
    document.getElementById('oauth-login-btn').addEventListener('click', oauthLogin);
    document.getElementById('cookie-login-btn').addEventListener('click', openCookieModal);
    // Cookie 登录模态框
    document.getElementById('cookie-submit-btn').addEventListener('click', cookieLogin);

    // 添加用户表单
    document.getElementById('add-user-form').addEventListener('submit', (e) => {
        e.preventDefault();
        const username = document.getElementById('new-username').value;
        const password = document.getElementById('new-password').value;
        addUser(username, password);
        e.target.reset();
    });

    // 修改密码表单
    document.getElementById('change-password-form').addEventListener('submit', (e) => {
        e.preventDefault();
        const oldPassword = document.getElementById('old-password').value;
        const newPassword = document.getElementById('new-password-change').value;
        changePassword(oldPassword, newPassword);
        e.target.reset();
    });

    // 日志刷新
    document.getElementById('refresh-logs-btn').addEventListener('click', loadLogs);
    document.getElementById('clear-logs-btn').addEventListener('click', () => {
        document.getElementById('log-content').textContent = '';
    });

    // 检查认证状态
    checkAuth();
});
