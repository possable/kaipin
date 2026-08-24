function getCSRFToken() {
    const el = document.querySelector('[name=csrfmiddlewaretoken]');
    return el ? el.value : '';
}

async function postJSON(url, data) {
    return fetch(url, {
        method: 'POST',
        headers: { 'X-CSRFToken': getCSRFToken() },
        body: data,
    });
}

// ============ Toast 通知 ============

// Toast 去重：最近 2 秒内相同 (message+type) 的 toast 不重复弹
const _recentToasts = new Map();
function showToast(message, type = 'success') {
    const key = `${type}::${message}`;
    const now = Date.now();
    const last = _recentToasts.get(key);
    if (last && now - last < 2000) return;
    _recentToasts.set(key, now);
    // 清理 5 秒前的旧记录
    for (const [k, t] of _recentToasts) {
        if (now - t > 5000) _recentToasts.delete(k);
    }
    const icons = { success: 'check-circle', error: 'x-circle', warning: 'exclamation-triangle' };
    const icon = icons[type] || 'info-circle';
    const toast = document.createElement('div');
    toast.className = `toast-message toast-${type}`;
    toast.innerHTML = `<i class="bi bi-${icon}"></i> ${message}`;
    toast.addEventListener('click', () => toast.remove());
    // 若有弹窗打开，则使用浮层模式，显示在弹窗之上；否则插到主内容顶部
    const modalOpen = document.querySelector('.modal-overlay[style*="display: flex"], .modal-overlay[style*="display:flex"]');
    if (modalOpen) {
        toast.classList.add('toast-floating');
        document.body.appendChild(toast);
    } else {
        const main = document.querySelector('.main-content');
        if (main) main.insertBefore(toast, main.firstChild);
        else document.body.appendChild(toast);
    }
    setTimeout(() => {
        if (toast.parentNode) toast.remove();
    }, 4000);
}

// ============ Loading 状态管理 ============

function disableButtons() {
    document.querySelectorAll('button').forEach(btn => {
        btn.disabled = true;
        btn.classList.add('btn-loading');
    });
}

function enableButtons() {
    document.querySelectorAll('button').forEach(btn => {
        btn.disabled = false;
        btn.classList.remove('btn-loading');
    });
}

// ============ 用户名→ID 映射 (O(1) Map) ============

let userMap = null;

function buildUserMap() {
    if (userMap) return userMap;
    userMap = new Map();
    if (typeof ALL_USERS !== 'undefined' && Array.isArray(ALL_USERS)) {
        ALL_USERS.forEach(u => userMap.set(u.name, u.id));
    }
    return userMap;
}

function findUserId(name) {
    if (!name) return '';
    const map = buildUserMap();
    return map.get(name) || '';
}

// 负责人字段即时保存（不需要层级校验，无需延迟）
async function saveAssigneeImmediate(el) {
    const url = pickUpdateUrl(el);
    if (!url) return;
    const uid = el.dataset.userid || '';
    const fd = new FormData();
    fd.append('field', 'assignee');
    fd.append('value', uid);
    try {
        const resp = await postJSON(url, fd);
        if (resp.ok) {
            showToast('负责人已更新', 'success');
        } else {
            let errMsg = `HTTP ${resp.status}`;
            try { const data = await resp.json(); errMsg = data.error || errMsg; } catch (_) {}
            showToast('保存失败: ' + errMsg, 'error');
        }
    } catch (e) {
        showToast('保存失败: 网络错误', 'error');
    }
}

// 根据元素上的 data-pk / data-stage-id / data-task-id 决定更新字段 URL
function pickUpdateUrl(el) {
    if (el.dataset.pk) return `/products/${el.dataset.pk}/update-field/`;
    if (el.dataset.stageId) return `/products/stages/${el.dataset.stageId}/update-field/`;
    if (el.dataset.taskId) return `/products/tasks/${el.dataset.taskId}/update-field/`;
    return '';
}

const TASK_STATUS_CLASSES = ['pending', 'in_progress', 'completed', 'overdue'];

// 前端校验：开始时间不能大于预计结束日期
// 找同一实体（同一 pk/stageId/taskId）的另一个日期字段，做前后对比
function validateDateOrder(el) {
    const entityKey = el.dataset.pk
        ? `pk-${el.dataset.pk}`
        : el.dataset.stageId
            ? `stage-${el.dataset.stageId}`
            : el.dataset.taskId
                ? `task-${el.dataset.taskId}`
                : '';
    if (!entityKey) return true;
    const container = el.closest('#product-modal-content, #stage-modal-content, #checklist-modal-content, form, .row, .stage-row, tr, body');
    if (!container) return true;
    // 找同实体的两个字段
    const startEl = container.querySelector(`[data-field="started_at"][data-${el.dataset.pk ? 'pk' : el.dataset.stageId ? 'stage-id' : 'task-id'}="${el.dataset.pk || el.dataset.stageId || el.dataset.taskId}"]`);
    const endEl = container.querySelector(`[data-field="expected_end_date"][data-${el.dataset.pk ? 'pk' : el.dataset.stageId ? 'stage-id' : 'task-id'}="${el.dataset.pk || el.dataset.stageId || el.dataset.taskId}"]`);
    if (!startEl || !endEl) return true;
    const startVal = startEl.value;
    const endVal = endEl.value;
    if (!startVal || !endVal) return true;
    // date 输入直接字符串比较也可（YYYY-MM-DD 天然有序）
    const startDate = startVal.slice(0, 10);
    const endDate = endVal.slice(0, 10);
    if (startDate > endDate) {
        showToast('开始时间不能大于预计结束日期', 'error');
        return false;
    }
    return true;
}

// 全局脏字段收集器：修改字段时标记 dirty，点保存时统一提交
// key = 元素引用，value = { url, field, value }
const DIRTY_FIELDS = new Map();

function markDirty(el, url, field, value) {
    DIRTY_FIELDS.set(el, { url, field, value });
    el.classList.add('is-dirty');
}

function clearDirty() {
    DIRTY_FIELDS.forEach((_, el) => el.classList.remove('is-dirty'));
    DIRTY_FIELDS.clear();
}

function hasDirty() {
    return DIRTY_FIELDS.size > 0;
}

// 内容片段加载完成后，根据片段里的 data-can-edit 隐藏标记控制对应 footer 的
// "保存并刷新"按钮是否显示。footerSelector 指向该弹窗的 footer 容器。
function applyEditPermissionToFooter(content, footerSelector) {
    const marker = content.querySelector('input[data-can-edit]');
    const canEdit = marker ? marker.dataset.canEdit === 'true' : true;
    const footer = document.querySelector(footerSelector);
    if (!footer) return;
    const saveBtn = footer.querySelector('.btn-save-reload');
    if (saveBtn) saveBtn.style.display = canEdit ? '' : 'none';
}

// 给指定根节点内的品/阶段/任务字段绑定变更处理。root 默认整个文档；
// 弹窗动态插入内容后需要对弹窗内容重新调用一次，因为新插入的元素还没绑定事件。
function bindFieldHandlers(root = document) {
    // 品字段变更（非用户搜索）—— 只标记 dirty，不立即保存
    root.querySelectorAll('.product-field:not(.user-search)').forEach(el => {
        el._prevValue = el.value;  // 记录上一次值，用于校验失败时回滚
        el.addEventListener('change', function() {
            const pk = this.dataset.pk;
            const field = this.dataset.field;
            if ((field === 'started_at' || field === 'expected_end_date') && !validateDateOrder(this)) {
                this.value = this._prevValue || '';
                return;
            }
            this._prevValue = this.value;
            markDirty(this, `/products/${pk}/update-field/`, field, this.value);
        });
    });

    // 用户搜索输入框（负责人）——即时保存（非时间字段，无需层级校验）
    root.querySelectorAll('.user-search').forEach(el => {
        let lastMatchedName = el.value || '';

        el.addEventListener('input', async function() {
            const name = this.value.trim();
            if (name === lastMatchedName) return;
            const uid = findUserId(name);
            if (!uid) return;
            lastMatchedName = name;
            this.dataset.userid = uid;
            await saveAssigneeImmediate(this);
        });

        el.addEventListener('blur', async function() {
            const name = this.value.trim();
            if (!name && this.dataset.userid) {
                this.dataset.userid = '';
                lastMatchedName = '';
                await saveAssigneeImmediate(this);
            } else if (name && name !== lastMatchedName && !findUserId(name)) {
                this.value = lastMatchedName;
            }
        });
    });

    // 阶段字段变更
    root.querySelectorAll('.stage-field').forEach(el => {
        el._prevValue = el.value;
        el.addEventListener('change', function() {
            const stageId = this.dataset.stageId;
            const field = this.dataset.field;
            if ((field === 'started_at' || field === 'expected_end_date') && !validateDateOrder(this)) {
                this.value = this._prevValue || '';
                return;
            }
            this._prevValue = this.value;
            markDirty(this, `/products/stages/${stageId}/update-field/`, field, this.value);
        });
    });

    // 任务字段变更（非用户搜索）
    root.querySelectorAll('.task-field:not(.user-search)').forEach(el => {
        el._prevValue = el.value;
        el.addEventListener('change', function() {
            const taskId = this.dataset.taskId;
            const field = this.dataset.field;
            if ((field === 'started_at' || field === 'expected_end_date') && !validateDateOrder(this)) {
                this.value = this._prevValue || '';
                return;
            }
            this._prevValue = this.value;
            markDirty(this, `/products/tasks/${taskId}/update-field/`, field, this.value);
        });
    });

    // 最小事项备注字段 —— 只标记 dirty，不立即保存
    root.querySelectorAll('.checklist-notes-field').forEach(el => {
        el._prevValue = el.value;
        el.addEventListener('change', function() {
            const itemId = this.dataset.itemId;
            this._prevValue = this.value;
            markDirty(this, `/products/checklist/${itemId}/save-notes/`, 'notes', this.value);
        });
    });

    // 文件上传 —— 成功后 DOM 插入附件链接，不刷新页面
    root.querySelectorAll('.file-upload-input').forEach(input => {
        input.addEventListener('change', async function() {
            const taskId = this.dataset.taskId;
            const file = this.files[0];
            if (!file) return;
            const formData = new FormData();
            formData.append('file', file);
            const resp = await postJSON(`/products/tasks/${taskId}/upload-attachment/`, formData);
            if (resp.ok) {
                const data = await resp.json();
                // 在 <label> 前面插入附件链接
                const label = this.parentElement;
                const link = document.createElement('a');
                link.href = data.url;
                link.target = '_blank';
                link.title = data.filename;
                link.innerHTML = '<i class="bi bi-paperclip"></i>';
                label.parentNode.insertBefore(link, label);
                this.value = ''; // 清除已选文件，允许再次上传同一文件
                showToast('文件上传成功', 'success');
            } else {
                const data = await resp.json();
                showToast('上传失败: ' + (data.error || '未知错误'), 'error');
            }
        });
    });
}

document.addEventListener('DOMContentLoaded', () => {
    // 预构建用户映射
    buildUserMap();
    bindFieldHandlers(document);

    // 页面加载后显示 sessionStorage 中的 toast（如有）
    const pendingToast = sessionStorage.getItem('toast_message');
    if (pendingToast) {
        try {
            const { message, type } = JSON.parse(pendingToast);
            showToast(message, type);
        } catch (e) { /* ignore */ }
        sessionStorage.removeItem('toast_message');
    }
});

// ============ 任务操作 ============

async function markTaskComplete(taskId, btn) {
    if (btn) { btn.disabled = true; }
    const resp = await postJSON(`/products/tasks/${taskId}/complete/`);
    if (btn) { btn.disabled = false; }
    if (resp.ok) {
        const data = await resp.json();
        if (data.reverted) {
            showToast('已撤销完成', 'success');
        } else {
            showToast('任务已完成', 'success');
        }
        if (isStageModalOpen()) { await refreshStageModal(); }
        else { location.reload(); }
    } else {
        const data = await resp.json();
        showToast('操作失败: ' + (data.error || '未知错误'), 'error');
    }
}

async function moveTask(taskId, direction, btn) {
    if (btn) { btn.disabled = true; }
    const resp = await postJSON(`/products/tasks/${taskId}/move/${direction}/`, new FormData());
    if (btn) { btn.disabled = false; }
    if (resp.ok) {
        if (isStageModalOpen()) { await refreshStageModal(); }
        else { location.reload(); }
    } else {
        const data = await resp.json();
        showToast('移动失败: ' + (data.error || '未知错误'), 'error');
    }
}

async function addTask(stageId) {
    const input = document.getElementById(`new-task-name-${stageId}`);
    const name = input.value.trim();
    if (!name) { showToast('请输入任务名称', 'warning'); return; }
    disableButtons();
    const formData = new FormData();
    formData.append('name', name);
    const resp = await postJSON(`/products/stages/${stageId}/add-task/`, formData);
    if (resp.ok) {
        if (isStageModalOpen()) { await refreshStageModal(); enableButtons(); }
        else location.reload();
    } else {
        enableButtons();
        const data = await resp.json();
        showToast('添加失败: ' + (data.error || '未知错误'), 'error');
    }
}

async function deleteTask(taskId) {
    if (!confirm('确定删除该任务？此操作不可撤销。')) return;
    disableButtons();
    const resp = await postJSON(`/products/tasks/${taskId}/delete/`);
    if (resp.ok) {
        if (isStageModalOpen()) { await refreshStageModal(); enableButtons(); }
        else location.reload();
    } else {
        enableButtons();
        const data = await resp.json();
        showToast('删除失败: ' + (data.error || '未知错误'), 'error');
    }
}

async function completeStage(stageId) {
    if (!confirm('确认该阶段所有任务已完成，进入下一阶段？')) return;
    disableButtons();
    const resp = await postJSON(`/products/stages/${stageId}/complete/`);
    if (resp.ok) {
        const data = await resp.json();
        if (data.product_completed) {
            // 暂存 toast 到 sessionStorage，reload 后显示
            sessionStorage.setItem('toast_message', JSON.stringify({
                message: '所有阶段已完成，该品已完结！',
                type: 'success'
            }));
        }
        location.reload();
    } else {
        enableButtons();
        const data = await resp.json();
        showToast('操作失败: ' + (data.error || '未知错误'), 'error');
    }
}

async function publishProduct(productId) {
    if (!confirm('确认发布？项目将正式开始，第一个阶段会被激活。')) return;
    disableButtons();
    const resp = await postJSON(`/products/${productId}/publish/`);
    if (resp.ok) {
        location.reload();
    } else {
        enableButtons();
        const data = await resp.json();
        showToast('操作失败: ' + (data.error || '未知错误'), 'error');
    }
}

async function cancelProduct(productId) {
    if (!confirm('确定取消该品？')) return;
    disableButtons();
    const resp = await postJSON(`/products/${productId}/cancel/`);
    if (resp.ok || resp.redirected) {
        window.location.href = '/';
    } else {
        enableButtons();
        const data = await resp.json();
        showToast('操作失败: ' + (data.error || '未知错误'), 'error');
    }
}

// 归档页彻底删除一个品（草稿/已完成/已取消），级联删除全部阶段/任务/附件/日志，不可撤销
async function deleteArchivedProduct(productId, productName, btn) {
    if (!confirm(`确定彻底删除 "${productName}"？此操作不可撤销，将同时删除其全部阶段、任务、附件和日志。`)) return;
    btn.disabled = true;
    const resp = await postJSON(`/products/${productId}/delete/`);
    if (resp.ok) {
        btn.closest('tr, .kanban-row').remove();
        showToast('已删除', 'success');
    } else {
        btn.disabled = false;
        const data = await resp.json();
        showToast('删除失败: ' + (data.error || '未知错误'), 'error');
    }
}

// 手动开始并行阶段
async function startStage(stageId) {
    disableButtons();
    const resp = await postJSON(`/products/stages/${stageId}/start/`);
    if (resp.ok) {
        location.reload();
    } else {
        enableButtons();
        const data = await resp.json();
        showToast('操作失败: ' + (data.error || '未知错误'), 'error');
    }
}

// ============ 阶段详情弹窗（看板点击某阶段时使用） ============

let currentStageModalId = null;
let currentProductModalId = null;
let currentProductModalUrl = null;

async function openStageModal(stageId, stageName) {
    currentStageModalId = stageId;
    const overlay = document.getElementById('stage-modal-overlay');
    const content = document.getElementById('stage-modal-content');
    document.getElementById('stage-modal-title').textContent = stageName;
    content.innerHTML = '<div class="stage-modal-loading"><i class="bi bi-hourglass-split"></i> 加载中...</div>';
    overlay.style.display = 'flex';

    const resp = await fetch(`/products/stages/${stageId}/detail-modal/`);
    if (resp.ok) {
        content.innerHTML = await resp.text();
        bindFieldHandlers(content);
        applyEditPermissionToFooter(content, '#stage-modal-overlay .modal-footer');
    } else {
        content.innerHTML = '<div class="stage-modal-loading">加载失败，请重试</div>';
    }
}

function closeStageModal(event) {
    if (event) event.stopPropagation();
    if (hasDirty() && !confirm('有未保存修改，确定关闭吗？')) return;
    clearDirty();
    document.getElementById('stage-modal-overlay').style.display = 'none';
    currentStageModalId = null;
    if (isProductModalOpen()) refreshProductModal();
}

function isStageModalOpen() {
    return currentStageModalId !== null
        && document.getElementById('stage-modal-overlay').style.display !== 'none';
}

async function loadProductModal(productId, productName, mode) {
    currentProductModalId = productId;
    const overlay = document.getElementById('product-modal-overlay');
    const content = document.getElementById('product-modal-content');
    const isProgress = mode === 'progress';
    currentProductModalUrl = isProgress
        ? `/products/${productId}/progress-modal/`
        : `/products/${productId}/info-modal/`;
    overlay.dataset.modalMode = mode;
    document.getElementById('product-modal-title').textContent = isProgress
        ? ''
        : productName + ' · 项目信息';
    content.innerHTML = '<div class="stage-modal-loading"><i class="bi bi-hourglass-split"></i> 加载中...</div>';
    const saveBtn = overlay.querySelector('.btn-save-reload');
    if (saveBtn) saveBtn.style.display = 'none';
    overlay.style.display = 'flex';

    const resp = await fetch(currentProductModalUrl);
    if (resp.ok) {
        content.innerHTML = await resp.text();
        bindFieldHandlers(content);
        applyEditPermissionToFooter(content, '#product-modal-overlay .modal-footer');
    } else {
        content.innerHTML = '<div class="stage-modal-loading">加载失败，请重试</div>';
    }
}

async function openProductProgressModal(productId, productName) {
    return loadProductModal(productId, productName, 'progress');
}

async function openProductModal(productId, productName) {
    return loadProductModal(productId, productName, 'info');
}

function closeProductModal(event) {
    if (event) event.stopPropagation();
    if (hasDirty() && !confirm('有未保存修改，确定关闭吗？')) return;
    clearDirty();
    document.getElementById('product-modal-overlay').style.display = 'none';
    currentProductModalId = null;
    currentProductModalUrl = null;
}

function isProductModalOpen() {
    return currentProductModalId !== null
        && document.getElementById('product-modal-overlay').style.display !== 'none';
}

function openAlertModal(productName, reasons) {
    document.getElementById('alert-modal-title').textContent = productName + ' · 异常原因';
    const content = document.getElementById('alert-modal-content');
    content.innerHTML = '<ul class="alert-modal-list">'
        + reasons.map(r => {
            const personHtml = r.person
                ? (r.canMessage
                    ? `<button type="button" class="person-link" onclick="openMessageModal(event, '${r.entityType}', ${r.entityId}, '${escapeHtml(r.person).replace(/'/g, "\\'")}')">${escapeHtml(r.person)}</button>`
                    : `<span>${escapeHtml(r.person)}</span>`)
                : '';
            return `<li>${escapeHtml(r.label)}${personHtml ? '（' + personHtml + '）' : ''}</li>`;
        }).join('')
        + '</ul>';
    document.getElementById('alert-modal-overlay').style.display = 'flex';
}

function closeAlertModal(event) {
    if (event) event.stopPropagation();
    document.getElementById('alert-modal-overlay').style.display = 'none';
}

async function openChecklistModal(taskId, taskName) {
    const overlay = document.getElementById('checklist-modal-overlay');
    const content = document.getElementById('checklist-modal-content');
    document.getElementById('checklist-modal-title').textContent = taskName;
    content.innerHTML = '<div class="stage-modal-loading"><i class="bi bi-hourglass-split"></i> 加载中...</div>';
    overlay.style.display = 'flex';

    const resp = await fetch(`/products/tasks/${taskId}/checklist-modal/`);
    if (resp.ok) {
        content.innerHTML = await resp.text();
        bindFieldHandlers(content);
        applyEditPermissionToFooter(content, '#checklist-modal-overlay .modal-footer');
    } else {
        content.innerHTML = '<div class="stage-modal-loading">加载失败，请重试</div>';
    }
}

function closeChecklistModal(event) {
    if (event) event.stopPropagation();
    if (hasDirty() && !confirm('有未保存修改，确定关闭吗？')) return;
    clearDirty();
    document.getElementById('checklist-modal-overlay').style.display = 'none';
    if (isStageModalOpen()) {
        refreshStageModal();
    } else if (isProductModalOpen()) {
        refreshProductModal();
    }
}

// 通用保存并刷新：收集所有 dirty 字段依次提交，全部完成后刷新
async function saveAndReload(btn) {
    if (btn) {
        btn.disabled = true;
        btn.innerHTML = '<i class="bi bi-hourglass-split"></i> 保存中...';
    }
    // 给当前打开的弹窗加半透明遮罩阻止误操作
    const openModal = btn && btn.closest('.modal-box');
    let overlay = null;
    if (openModal) {
        overlay = document.createElement('div');
        overlay.className = 'modal-saving-overlay';
        overlay.innerHTML = '<i class="bi bi-hourglass-split"></i> 保存中...';
        openModal.appendChild(overlay);
    }
    // 让当前焦点元素失焦，可能触发一次 change/input 事件标记 dirty
    const active = document.activeElement;
    if (active && typeof active.blur === 'function') active.blur();
    await new Promise(r => setTimeout(r, 100));

    // 串行提交所有 dirty 字段：同实体字段有前后依赖（如 started_at vs expected_end_date），
    // 并发会因层级校验用旧值而失败，串行保证每次校验用的都是最新 DB 状态。
    // 优先提交 expected_end_date，再提交 started_at，避免"扩大时间范围时开始时间被旧结束时间卡住"
    const entries = Array.from(DIRTY_FIELDS.entries())
        .filter(([, info]) => info.url)
        .sort(([, a], [, b]) => {
            const order = {expected_end_date: 0, started_at: 1};
            return (order[a.field] ?? 2) - (order[b.field] ?? 2);
        });
    const failures = [];
    for (const [el, info] of entries) {
        const fd = new FormData();
        if (info.field === 'notes') {
            // checklist_item_save_notes 接口用专属参数名 notes，不是通用的 field/value
            fd.append('notes', info.value);
        } else {
            fd.append('field', info.field);
            fd.append('value', info.value);
        }
        try {
            const resp = await postJSON(info.url, fd);
            if (!resp.ok) {
                let errMsg = `HTTP ${resp.status}`;
                try { const data = await resp.json(); errMsg = data.error || errMsg; } catch (_) {}
                console.error('字段保存失败:', info, errMsg);
                failures.push(`${info.field}: ${errMsg}`);
            }
        } catch (e) {
            console.error('字段保存异常:', info, e);
            failures.push(`${info.field}: 网络错误`);
        }
    }

    if (failures.length > 0) {
        showToast(`保存失败: ${failures[0]}`, 'error');
        if (btn) {
            btn.disabled = false;
            btn.innerHTML = '<i class="bi bi-check-lg"></i> 保存并刷新';
        }
        if (overlay && overlay.parentNode) overlay.parentNode.removeChild(overlay);
        return;
    }
    clearDirty();
    showToast('已保存', 'success');
    setTimeout(() => location.reload(), 300);
}

async function saveProductProfile(event, form) {
    event.preventDefault();
    const pk = form.dataset.pk;
    const resp = await postJSON(`/products/${pk}/update-profile/`, new FormData(form));
    if (resp.ok) {
        showToast('产品资料已保存');
        if (form.closest('#product-modal-overlay')) {
            // 已经保存，清空 dirty 避免关闭时的未保存提示
            clearDirty();
            closeProductModal();
        }
    } else {
        const data = await resp.json();
        showToast('保存失败: ' + (data.error || '未知错误'), 'error');
    }
    return false;
}

// 弹窗内做了不影响看板整体流水线结构的操作（如任务增删/完成）后，只刷新弹窗内容，不整页刷新
async function refreshStageModal() {
    if (!isStageModalOpen()) return;
    const content = document.getElementById('stage-modal-content');
    const resp = await fetch(`/products/stages/${currentStageModalId}/detail-modal/`);
    if (resp.ok) {
        content.innerHTML = await resp.text();
        bindFieldHandlers(content);
    }
}

async function refreshProductModal() {
    if (!isProductModalOpen() || !currentProductModalUrl) return;
    const content = document.getElementById('product-modal-content');
    const resp = await fetch(currentProductModalUrl);
    if (resp.ok) {
        content.innerHTML = await resp.text();
        bindFieldHandlers(content);
        applyEditPermissionToFooter(content, '#product-modal-overlay .modal-footer');
    }
}

function getProjectProgressExportArea() {
    return document.querySelector('#product-modal-content #project-progress-export-area');
}

function projectProgressExportFilename(extension) {
    const area = getProjectProgressExportArea();
    const projectName = area?.dataset.exportTitle || '项目';
    const dateText = new Date().toISOString().slice(0, 10);
    const safeName = `${projectName}_项目进度总览_${dateText}`
        .replace(/[\\/:*?"<>|]/g, '_')
        .replace(/\s+/g, ' ')
        .trim();
    return `${safeName}.${extension}`;
}

async function exportProjectProgressPng(button) {
    const source = getProjectProgressExportArea();
    if (!source || !source.dataset.exportUrl) {
        showToast('未找到项目进度总览导出地址', 'error');
        return;
    }

    const originalHtml = button?.innerHTML;
    if (button) {
        button.disabled = true;
        button.innerHTML = '<i class="bi bi-hourglass-split"></i> 生成中...';
    }

    try {
        const response = await fetch(source.dataset.exportUrl);
        if (!response.ok) {
            let message = '服务器生成图片失败';
            try {
                const data = await response.json();
                if (data.error) message = data.error;
            } catch (error) {
                // 非 JSON 错误响应使用通用提示。
            }
            throw new Error(message);
        }

        const pngBlob = await response.blob();
        if (!pngBlob.size) throw new Error('服务器返回的图片为空');
        const downloadUrl = URL.createObjectURL(pngBlob);
        try {
            const link = document.createElement('a');
            link.href = downloadUrl;
            link.download = projectProgressExportFilename('png');
            document.body.appendChild(link);
            link.click();
            link.remove();
            showToast('项目进度总览 PNG 已导出', 'success');
        } finally {
            URL.revokeObjectURL(downloadUrl);
        }
    } catch (error) {
        console.error('导出项目进度总览 PNG 失败:', error);
        showToast(`导出失败：${error.message || '请稍后重试'}`, 'error');
    } finally {
        if (button) {
            button.disabled = false;
            button.innerHTML = originalHtml;
        }
    }
}

function printProjectProgressOverview(button) {
    const source = getProjectProgressExportArea();
    if (!source) {
        showToast('未找到可打印的项目进度总览', 'error');
        return;
    }

    const printWindow = window.open('', '_blank', 'width=1400,height=900');
    if (!printWindow) {
        showToast('浏览器阻止了打印窗口，请允许弹出窗口后重试', 'error');
        return;
    }

    const styleMarkup = Array.from(document.querySelectorAll('link[rel="stylesheet"], style'))
        .map(element => element.outerHTML)
        .join('\n');
    const title = source.dataset.exportTitle || '项目';
    printWindow.addEventListener('load', () => {
        setTimeout(() => {
            printWindow.focus();
            printWindow.print();
        }, 300);
    }, { once: true });
    printWindow.addEventListener('afterprint', () => printWindow.close(), { once: true });
    printWindow.document.open();
    printWindow.document.write(`<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<title>${escapeHtml(title)} · 项目进度总览</title>
${styleMarkup}
<style>
@page { size: landscape; margin: 8mm; }
html, body { margin: 0; padding: 0; background: #f5f7fa; color-scheme: light; }
body { padding: 8px; -webkit-print-color-adjust: exact; print-color-adjust: exact; }
.roadmap-print-root { position: static !important; inset: auto !important; display: block !important; min-height: 0 !important; background: transparent !important; }
.roadmap-print-title { display: none; }
.project-progress-overview { margin: 0 auto; box-shadow: none; }
.roadmap-summary-card, .roadmap-stage-row, .project-risk-item { break-inside: avoid; }
.roadmap-timeline-card, .project-risk-panel { break-inside: auto; }
.roadmap-export-actions { display: none !important; }
button { cursor: default !important; }
@media print { body { padding: 0; } }
</style>
</head>
<body><div id="product-modal-overlay" class="roadmap-print-root" data-modal-mode="progress">${source.outerHTML}</div></body>
</html>`);
    printWindow.document.close();
    if (button) button.blur();
}

// 阶段折叠/展开
function toggleStage(header) {
    const row = header.parentElement;
    const body = row.querySelector('.stage-row-body');
    const chevron = header.querySelector('.stage-chevron');
    body.classList.toggle('show');
    chevron.classList.toggle('open');
}

// ============ 最小事项清单 ============

// 展开/收起某任务的最小事项清单行
function toggleChecklist(btn, taskId) {
    const row = document.querySelector(`.checklist-row[data-task-id="${taskId}"]`);
    if (!row) return;
    const chevron = btn.querySelector('.checklist-chevron');
    const isShown = row.style.display !== 'none';
    row.style.display = isShown ? 'none' : '';
    chevron.classList.toggle('open', !isShown);
}

// 展开/收起某条最小事项下的日志区
function toggleChecklistLogs(btn, itemId) {
    const item = document.querySelector(`.checklist-item[data-item-id="${itemId}"]`);
    if (!item) return;
    const logs = item.querySelector('.checklist-logs');
    logs.style.display = logs.style.display === 'none' ? '' : 'none';
}

async function addChecklistItem(taskId, btn) {
    const row = btn.closest('.checklist-box');
    const input = row.querySelector('.checklist-new-input');
    const name = input.value.trim();
    if (!name) { showToast('请输入事项名称', 'warning'); return; }
    const formData = new FormData();
    formData.append('name', name);
    const resp = await postJSON(`/products/tasks/${taskId}/checklist/add/`, formData);
    const data = await resp.json();
    if (resp.ok) {
        const itemsBox = row.querySelector('.row');
        itemsBox.insertAdjacentHTML('beforeend', renderChecklistItem(data.id, data.name));
        input.value = '';
    } else {
        showToast('添加失败: ' + (data.error || '未知错误'), 'error');
    }
}

function renderChecklistItem(id, name) {
    return `
    <div class="col-md-6 checklist-grid-item" data-item-id="${id}">
        <label class="form-label">${escapeHtml(name)}</label>
        <input type="text" class="form-input checklist-notes-field" name="notes_${id}" placeholder="填写内容..." style="font-size:12px;">
    </div>`;
}

async function saveAllChecklistNotes(taskId) {
    const formData = new FormData();
    document.querySelectorAll('.checklist-notes-field').forEach(el => {
        formData.append(el.name, el.value);
    });
    const btn = document.querySelector('#checklist-modal-content .btn-primary');
    btn.disabled = true;
    const resp = await postJSON(`/products/tasks/${taskId}/save-all-notes/`, formData);
    btn.disabled = false;
    if (resp.ok) {
        showToast('已保存');
    } else {
        const data = await resp.json();
        showToast('保存失败: ' + (data.error || '未知错误'), 'error');
    }
}

async function saveChecklistNotes(itemId, btn) {
    const item = btn.closest('.checklist-grid-item');
    const textarea = item.querySelector('textarea');
    const notes = textarea.value.trim();
    const formData = new FormData();
    formData.append('notes', notes);
    const resp = await postJSON(`/products/checklist/${itemId}/save-notes/`, formData);
    if (resp.ok) {
        showToast('已保存');
    } else {
        const data = await resp.json();
        showToast('保存失败: ' + (data.error || '未知错误'), 'error');
    }
}

function escapeHtml(str) {
    const div = document.createElement('div');
    div.textContent = str;
    return div.innerHTML;
}

async function toggleChecklistItem(itemId, checkbox) {
    checkbox.disabled = true;
    const resp = await postJSON(`/products/checklist/${itemId}/toggle/`);
    const data = await resp.json();
    checkbox.disabled = false;
    if (resp.ok) {
        checkbox.checked = data.is_done;
        const label = checkbox.parentElement.querySelector('label');
        if (label) label.style.textDecoration = data.is_done ? 'line-through' : '';
        if (label) label.style.color = data.is_done ? 'var(--color-gray-400)' : '';
    } else {
        checkbox.checked = !checkbox.checked;
        showToast('操作失败: ' + (data.error || '未知错误'), 'error');
    }
}

async function deleteChecklistItem(itemId, btn) {
    if (!confirm('确定删除该事项？')) return;
    const resp = await postJSON(`/products/checklist/${itemId}/delete/`);
    if (resp.ok) {
        btn.closest('.checklist-grid-item').remove();
    } else {
        const data = await resp.json();
        showToast('删除失败: ' + (data.error || '未知错误'), 'error');
    }
}

async function addChecklistLog(itemId, btn) {
    const item = btn.closest('.checklist-item');
    const logsBox = item.querySelector('.checklist-logs');
    const input = item.querySelector('.checklist-log-input');
    const content = input.value.trim();
    if (!content) { showToast('请输入日志内容', 'warning'); return; }
    const formData = new FormData();
    formData.append('content', content);
    const resp = await postJSON(`/products/checklist/${itemId}/log/add/`, formData);
    const data = await resp.json();
    if (resp.ok) {
        const entry = document.createElement('div');
        entry.className = 'checklist-log-entry';
        entry.innerHTML = `<span class="log-meta">${escapeHtml(data.user_name)} · ${data.created_at.slice(5)}</span><span class="log-content">${escapeHtml(data.content)}</span>`;
        logsBox.appendChild(entry);
        input.value = '';
        const countSpan = item.querySelector('.checklist-log-count');
        const count = logsBox.querySelectorAll('.checklist-log-entry').length;
        countSpan.textContent = count + '条记录';
    } else {
        showToast('记录失败: ' + (data.error || '未知错误'), 'error');
    }
}

// ============ 发送企业微信消息弹窗 ============
// 权限与编辑权限同步：发消息按钮对应品/阶段/任务这三种实体之一，
// 后端会按该实体当前的负责人和调用者的编辑权限重新校验，而不是只信任前端传的用户名

let messageModalEntityType = null;
let messageModalEntityId = null;

function openMessageModal(event, entityType, entityId, userName) {
    if (event) { event.preventDefault(); event.stopPropagation(); }
    messageModalEntityType = entityType;
    messageModalEntityId = entityId;
    document.getElementById('message-modal-target-name').textContent = userName;
    const textarea = document.getElementById('message-modal-content');
    textarea.value = '';
    document.getElementById('message-modal-overlay').style.display = 'flex';
    textarea.focus();
}

// 回车发送，Shift+回车换行
document.addEventListener('DOMContentLoaded', () => {
    const textarea = document.getElementById('message-modal-content');
    if (!textarea) return;
    textarea.addEventListener('keydown', function(e) {
        if (e.key === 'Enter' && !e.shiftKey) {
            e.preventDefault();
            sendPersonMessage();
        }
    });
});

function closeMessageModal(event) {
    if (event) event.stopPropagation();
    document.getElementById('message-modal-overlay').style.display = 'none';
    messageModalEntityType = null;
    messageModalEntityId = null;
}

async function sendPersonMessage() {
    const content = document.getElementById('message-modal-content').value.trim();
    if (!content) { showToast('请输入消息内容', 'warning'); return; }
    if (!messageModalEntityType || !messageModalEntityId) return;

    const targetName = document.getElementById('message-modal-target-name').textContent || '';
    const btn = document.getElementById('message-modal-send-btn');
    btn.disabled = true;
    const formData = new FormData();
    formData.append('content', content);
    const resp = await postJSON(`/reminders/send/${messageModalEntityType}/${messageModalEntityId}/`, formData);
    const data = await resp.json();
    btn.disabled = false;
    if (resp.ok) {
        showToast(`消息已发送给 ${targetName}`, 'success');
        closeMessageModal();
    } else {
        showToast('发送失败: ' + (data.error || '未知错误'), 'error');
    }
}

// 从看板点击阶段跳转过来时，自动展开并滚动到对应阶段
document.addEventListener('DOMContentLoaded', () => {
    if (!window.location.hash.startsWith('#stage-')) return;
    const row = document.querySelector(window.location.hash);
    if (!row) return;
    const body = row.querySelector('.stage-row-body');
    const chevron = row.querySelector('.stage-chevron');
    if (body && !body.classList.contains('show')) {
        body.classList.add('show');
        if (chevron) chevron.classList.add('open');
    }
    row.scrollIntoView({ block: 'center' });
});

// 浏览器返回键：优先关闭最上层弹窗，而不是离开页面
// 简化实现：只在 popstate 时如果有弹窗打开就先关，不做 pushState 干预
window.addEventListener('popstate', function(e) {
    const MODAL_IDS = ['checklist-modal-overlay', 'stage-modal-overlay', 'product-modal-overlay', 'alert-modal-overlay', 'message-modal-overlay'];
    for (const id of MODAL_IDS) {
        const el = document.getElementById(id);
        if (el && el.style.display === 'flex') {
            el.style.display = 'none';
            // 推回历史，防止真的离开页面
            history.pushState(null, '', location.href);
            return;
        }
    }
});

// ============ 个人待办事项 ============

function toggleTodoAddForm(event) {
    if (event) event.stopPropagation();
    const form = document.getElementById('todo-add-form');
    if (!form) return;
    const showing = form.style.display !== 'none';
    form.style.display = showing ? 'none' : 'block';
    if (!showing) {
        const input = document.getElementById('todo-content-input');
        if (input) { input.value = ''; input.focus(); }
        const due = document.getElementById('todo-due-input');
        if (due) due.value = '';
    }
}

function handleTodoInputKey(event) {
    if (event.key === 'Enter') { event.preventDefault(); submitTodo(); }
    else if (event.key === 'Escape') { event.preventDefault(); toggleTodoAddForm(); }
}

async function submitTodo() {
    const content = document.getElementById('todo-content-input').value.trim();
    const dueAt = document.getElementById('todo-due-input').value;
    if (!content) { showToast('请输入待办内容', 'warning'); return; }
    const fd = new FormData();
    fd.append('content', content);
    if (dueAt) fd.append('due_at', dueAt);
    const resp = await postJSON('/accounts/todos/add/', fd);
    const data = await resp.json();
    if (resp.ok) {
        // DOM 插入新条目到列表首位
        const list = document.getElementById('todo-list');
        const empty = list.querySelector('.todo-empty');
        if (empty) empty.remove();
        const dueSpan = data.due_at
            ? `<span class="todo-time">${escapeHtml(data.due_at.slice(5))}</span>` : '';
        const html = `<label class="todo-item" data-todo-id="${data.id}">
            <input type="checkbox" onchange="toggleTodo(${data.id}, this)">
            <span class="todo-text">${escapeHtml(data.content)}</span>
            ${dueSpan}
            <button type="button" class="todo-delete-btn" title="删除" onclick="deleteTodo(${data.id}, event)">×</button>
        </label>`;
        list.insertAdjacentHTML('afterbegin', html);
        toggleTodoAddForm();
        showToast('已添加');
    } else {
        showToast('添加失败: ' + (data.error || '未知错误'), 'error');
    }
}

async function toggleTodo(todoId, checkbox) {
    checkbox.disabled = true;
    const resp = await postJSON(`/accounts/todos/${todoId}/toggle/`, new FormData());
    const data = await resp.json();
    checkbox.disabled = false;
    if (resp.ok) {
        const item = checkbox.closest('.todo-item');
        if (item) item.classList.toggle('is-done', data.is_done);
        checkbox.checked = data.is_done;
    } else {
        checkbox.checked = !checkbox.checked;
        showToast('操作失败: ' + (data.error || '未知错误'), 'error');
    }
}

async function deleteTodo(todoId, event) {
    if (event) { event.preventDefault(); event.stopPropagation(); }
    if (!confirm('确定删除该待办？')) return;
    const resp = await postJSON(`/accounts/todos/${todoId}/delete/`, new FormData());
    if (resp.ok) {
        const item = document.querySelector(`.todo-item[data-todo-id="${todoId}"]`);
        if (item) item.remove();
        // 如果删完了显示空状态
        const list = document.getElementById('todo-list');
        if (list && !list.querySelector('.todo-item')) {
            list.innerHTML = '<div class="todo-empty" style="padding:16px 12px;text-align:center;color:var(--muted);font-size:12px;">暂无待办，点击右上角 + 添加</div>';
        }
    } else {
        const data = await resp.json();
        showToast('删除失败: ' + (data.error || '未知错误'), 'error');
    }
}
