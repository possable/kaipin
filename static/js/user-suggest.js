// 人员输入框的候选下拉：输入第一个字之后才展示匹配的人。
//
// 原生 <datalist> 的行为是聚焦/清空时把所有选项铺出来，人员一多就是一面墙。
// 这里接管所有 .user-search 输入框：移除 list 属性（关掉原生弹层），
// 改成自己渲染的候选面板，只有输入了至少一个字符才显示，并且按输入内容过滤。
//
// 候选来源仍然是页面上的 <datalist>（各页面已有的数据不用重写），
// 找不到时退回全局 ALL_USERS。选择后回填 input 值、data-userid，
// 再派发 input/change，页面里原有「选人后同步隐藏 id / 立即保存」的逻辑照常工作。
(function () {
    'use strict';

    const MAX_ITEMS = 8;
    let panel = null;
    let activeInput = null;
    let items = [];
    let activeIndex = -1;

    function ensurePanel() {
        if (panel) return panel;
        panel = document.createElement('div');
        panel.className = 'user-suggest-panel';
        panel.hidden = true;
        panel.setAttribute('role', 'listbox');
        // mousedown 里 preventDefault，避免点选项时输入框先失焦把面板关掉
        panel.addEventListener('mousedown', function (event) {
            event.preventDefault();
        });
        panel.addEventListener('click', function (event) {
            const item = event.target.closest('.user-suggest-item');
            if (!item || item.dataset.index === undefined) return;
            choose(Number(item.dataset.index));
        });
        document.body.appendChild(panel);
        return panel;
    }

    // 从页面上的 datalist 收集候选人。多个 datalist 可能同时存在（看板一份、
    // 弹窗一份），内容一样，按「名字 + id」去重即可。
    function collectUsers(el) {
        const listIds = [];
        if (el.dataset.suggestList) listIds.push(el.dataset.suggestList);
        listIds.push('users-datalist', 'modal-users-datalist');

        const seenLists = new Set();
        const seenUsers = new Set();
        const users = [];

        listIds.forEach(function (id) {
            const list = document.getElementById(id);
            if (!list || seenLists.has(list)) return;
            seenLists.add(list);
            list.querySelectorAll('option').forEach(function (opt) {
                const name = (opt.value || '').trim();
                if (!name) return;
                const uid = opt.dataset.id || opt.getAttribute('data-id') || '';
                const key = name + '\u0000' + uid;
                if (seenUsers.has(key)) return;
                seenUsers.add(key);
                users.push({ name: name, id: uid });
            });
        });

        if (!users.length && typeof ALL_USERS !== 'undefined' && Array.isArray(ALL_USERS)) {
            ALL_USERS.forEach(function (u) {
                const name = (u.name || '').trim();
                if (!name) return;
                const key = name + '\u0000' + (u.id || '');
                if (seenUsers.has(key)) return;
                seenUsers.add(key);
                users.push({ name: name, id: String(u.id || '') });
            });
        }
        return users;
    }

    function filterUsers(el, query) {
        const q = query.toLowerCase();
        const all = collectUsers(el);
        const starts = [];
        const contains = [];
        all.forEach(function (u) {
            const name = u.name.toLowerCase();
            if (name.indexOf(q) === 0) starts.push(u);
            else if (name.indexOf(q) !== -1) contains.push(u);
        });
        return starts.concat(contains).slice(0, MAX_ITEMS);
    }

    function positionPanel(el) {
        const rect = el.getBoundingClientRect();
        const width = Math.max(rect.width, 160);
        let top = rect.bottom + 4;
        // 下方空间不够就翻到输入框上方
        const panelHeight = panel.offsetHeight || 0;
        if (top + panelHeight > window.innerHeight - 8) {
            top = Math.max(8, rect.top - panelHeight - 4);
        }
        panel.style.left = Math.max(8, rect.left) + 'px';
        panel.style.top = top + 'px';
        panel.style.width = width + 'px';
    }

    function render() {
        panel.innerHTML = '';
        if (!items.length) {
            const empty = document.createElement('div');
            empty.className = 'user-suggest-empty';
            empty.textContent = '无匹配人员';
            panel.appendChild(empty);
            return;
        }
        items.forEach(function (u, index) {
            const item = document.createElement('button');
            item.type = 'button';
            item.className = 'user-suggest-item' + (index === activeIndex ? ' is-active' : '');
            item.dataset.index = String(index);
            item.setAttribute('role', 'option');
            item.textContent = u.name;
            panel.appendChild(item);
        });
    }

    function open(el, query) {
        ensurePanel();
        activeInput = el;
        items = filterUsers(el, query);
        activeIndex = items.length ? 0 : -1;
        render();
        panel.hidden = false;
        positionPanel(el);
    }

    function hide() {
        if (panel) panel.hidden = true;
        activeInput = null;
        items = [];
        activeIndex = -1;
    }

    function choose(index) {
        const el = activeInput;
        if (!el || index < 0 || index >= items.length) return;
        const picked = items[index];
        el.value = picked.name;
        if (picked.id) el.dataset.userid = picked.id;
        hide();
        // 让页面原有的 input/change 监听（同步隐藏 id、立即保存负责人）跑起来；
        // 用自己的 input 监听要跳过，否则刚收起的面板会被这次派发重新打开
        el._userSuggestChoosing = true;
        el.dispatchEvent(new Event('input', { bubbles: true }));
        el.dispatchEvent(new Event('change', { bubbles: true }));
        el._userSuggestChoosing = false;
        el.focus();
    }

    function moveActive(delta) {
        if (!items.length) return;
        activeIndex = (activeIndex + delta + items.length) % items.length;
        render();
        positionPanel(activeInput);
    }

    function init(el) {
        if (el._userSuggestReady) return;
        el._userSuggestReady = true;

        const listId = el.getAttribute('list');
        if (listId) el.dataset.suggestList = listId;
        // 关掉原生 datalist 弹层：它在聚焦/空值时会把全部人员铺出来
        el.removeAttribute('list');
        if (!el.getAttribute('autocomplete')) el.setAttribute('autocomplete', 'off');

        el.addEventListener('input', function () {
            if (el._userSuggestChoosing) return;
            const query = el.value.trim();
            if (!query) {
                hide();
                return;
            }
            open(el, query);
        });

        el.addEventListener('keydown', function (event) {
            if (!activeInput || activeInput !== el || !panel || panel.hidden) return;
            if (event.key === 'ArrowDown') {
                event.preventDefault();
                if (items.length) moveActive(1);
            } else if (event.key === 'ArrowUp') {
                event.preventDefault();
                if (items.length) moveActive(-1);
            } else if (event.key === 'Enter') {
                if (activeIndex >= 0 && items.length) {
                    event.preventDefault();
                    choose(activeIndex);
                }
            } else if (event.key === 'Escape') {
                event.preventDefault();
                hide();
            } else if (event.key === 'Tab') {
                hide();
            }
        });

        el.addEventListener('blur', function () {
            // 点击选项时 mousedown 已阻止失焦，这里只处理真正离开输入框的情况
            window.setTimeout(function () {
                if (activeInput === el) hide();
            }, 120);
        });
    }

    function initAll(root) {
        root.querySelectorAll('input.user-search').forEach(init);
    }

    function bootstrap() {
        initAll(document);
        // 动态插入的弹窗内容也能生效：聚焦时按需初始化
        document.addEventListener('focusin', function (event) {
            const el = event.target.closest && event.target.closest('input.user-search');
            if (el) init(el);
        });
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', bootstrap);
    } else {
        bootstrap();
    }

    // 面板挂在 body 上，页面滚动/缩放后位置会错，直接收起；
    // 但面板自己内部滚动（候选超过一屏）不能把自己关掉
    window.addEventListener('scroll', function (event) {
        if (panel && !panel.hidden && panel.contains(event.target)) return;
        hide();
    }, true);
    window.addEventListener('resize', hide);
    document.addEventListener('mousedown', function (event) {
        if (!panel || panel.hidden) return;
        if (panel.contains(event.target)) return;
        if (activeInput && activeInput.contains(event.target)) return;
        hide();
    });
})();
