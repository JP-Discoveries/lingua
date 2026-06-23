(function () {
    'use strict';

    /* ═══════════════════════════════════════════════════════════════════════
       State
    ═══════════════════════════════════════════════════════════════════════ */
    const state = {
        view:      'home',
        stack:     [],        // [{view, scrollTop, word?}]
        word:      null,      // current word object
        favorites: [],
        history:   [],
        wotd:      null,
        llmReady:    false,
        llmLoading:  false,
        llmActive:   false,
        llmBuf:      '',
        llmGen:      0,       // incremented on each new word/stream to orphan stale events
        _tokenHandler: null,
        _thinkHandler: null,
    };

    /* ═══════════════════════════════════════════════════════════════════════
       Tiny helpers
    ═══════════════════════════════════════════════════════════════════════ */
    const $ = id => document.getElementById(id);

    function esc(s) {
        return String(s)
            .replace(/&/g, '&amp;')
            .replace(/</g, '&lt;')
            .replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;');
    }

    function invoke(method, args = []) {
        return window.orbitInvoke(method, args).then(res => res?.result ?? res);
    }

    /* ── Toast ────────────────────────────────────────────────────────────── */
    let _toastTimer;
    function toast(msg) {
        const el = $('toast');
        el.textContent = msg;
        el.classList.add('show');
        clearTimeout(_toastTimer);
        _toastTimer = setTimeout(() => el.classList.remove('show'), 2600);
    }

    /* ═══════════════════════════════════════════════════════════════════════
       Navigation
    ═══════════════════════════════════════════════════════════════════════ */
    function navigate(to, data = {}, isBack = false) {
        if (to === state.view) return;

        const fromEl = $('view-' + state.view);
        const toEl   = $('view-' + to);

        if (!isBack) state.stack.push({ view: state.view, scrollTop: fromEl.scrollTop || 0 });

        // Render content before the view is visible
        if (to === 'results')   renderResults(data.results || [], data.query || '');
        if (to === 'word')      renderWord(data.word);
        if (to === 'favorites') renderFavorites();
        if (to === 'home')      renderHistory();

        // Slide-in animation
        const dx = isBack ? '-36px' : '36px';
        toEl.style.cssText = `transform:translateX(${dx});opacity:0;`;
        toEl.hidden = false;

        requestAnimationFrame(() => requestAnimationFrame(() => {
            const t = 'opacity 230ms cubic-bezier(.4,0,.2,1), transform 230ms cubic-bezier(.4,0,.2,1)';
            fromEl.style.cssText = `transition:${t};opacity:0;transform:translateX(${isBack ? '36px' : '-36px'})`;
            toEl.style.cssText   = `transition:${t};opacity:1;transform:translateX(0)`;

            setTimeout(() => {
                fromEl.hidden = true;
                fromEl.style.cssText = '';
                toEl.style.cssText   = '';
                if (isBack && data.scrollTop) toEl.querySelector('.view-body')?.scrollTo(0, data.scrollTop);
                state.view = to;
            }, 240);
        }));
    }

    function goHome() {
        if (state.view === 'home') return;
        state.stack = [];
        state.llmGen++;
        state.llmActive = false;
        navigate('home', {}, true);
    }

    function goBack() {
        if (!state.stack.length) return;
        const prev = state.stack.pop();
        // Chip→chip back: same view, re-render the saved word in place
        if (prev.view === 'word' && state.view === 'word' && prev.word) {
            state.word = prev.word;
            renderWord(prev.word);
            setTimeout(() => { $('word-content').scrollTop = prev.scrollTop || 0; }, 0);
            return;
        }
        navigate(prev.view, { scrollTop: prev.scrollTop, word: prev.word }, true);
    }

    /* ═══════════════════════════════════════════════════════════════════════
       Home
    ═══════════════════════════════════════════════════════════════════════ */
    async function initHome() {
        const [wotdRes, histRes, favRes] = await Promise.all([
            invoke('getWordOfDay'),
            invoke('getHistory'),
            invoke('getFavorites'),
        ]);
        state.wotd      = wotdRes;
        state.history   = (histRes.history   || []);
        state.favorites = (favRes.favorites  || []);
        renderWotd();
        renderHistory();
    }

    function renderWotd() {
        const card = $('wotd-card');
        const w = state.wotd;
        if (!w || !w.found) {
            card.innerHTML = '<p class="empty-msg">Could not load word of the day.</p>';
            return;
        }
        const entry = w.entries[0];
        card.innerHTML = `
            <div class="wotd-eyebrow">Word of the Day</div>
            <div class="wotd-word">${esc(w.word)}</div>
            ${w.phonetic ? `<div class="wotd-phonetic">${esc(w.phonetic)}</div>` : ''}
            <div class="wotd-pos">${esc(entry.pos)}</div>
            <p class="wotd-def">${esc(entry.definitions[0].text)}</p>
            <button id="wotd-explore" class="btn-explore">Explore word →</button>
        `;
        $('wotd-explore').addEventListener('click', () => openWord(w.word));
    }

    function renderHistory() {
        const sec = $('home-history-section');
        const row = $('home-history');
        if (!state.history.length) { sec.hidden = true; return; }
        sec.hidden = false;
        row.innerHTML = state.history.slice(0, 14).map(w =>
            `<button class="chip chip-hist" data-word="${esc(w)}">${esc(w)}</button>`
        ).join('');
        row.querySelectorAll('.chip-hist').forEach(b =>
            b.addEventListener('click', () => openWord(b.dataset.word))
        );
    }

    /* ═══════════════════════════════════════════════════════════════════════
       Search
    ═══════════════════════════════════════════════════════════════════════ */
    async function search(query) {
        const q = query.trim();
        if (!q) return;
        const res = await invoke('searchWords', [q]);
        navigate('results', { results: res.results || [], query: res.query || q });
    }

    function wireSearch(inputId) {
        $(inputId).addEventListener('keydown', e => {
            if (e.key === 'Enter') search(e.target.value);
        });
    }

    /* ═══════════════════════════════════════════════════════════════════════
       Results
    ═══════════════════════════════════════════════════════════════════════ */
    function renderResults(results, query) {
        $('results-search').value = query;
        $('results-heading').textContent = results.length
            ? `${results.length} result${results.length === 1 ? '' : 's'} for "${query}"`
            : `No results for "${query}"`;

        const list = $('results-list');
        if (!results.length) {
            list.innerHTML = '<p class="empty-msg">Try a different spelling or a broader term.</p>';
            return;
        }
        list.innerHTML = results.map(r => `
            <div class="result-item" data-word="${esc(r.word)}">
                <div class="result-row">
                    <span class="result-word">${esc(r.word)}</span>
                    <span class="pos-badge">${esc(r.pos)}</span>
                </div>
                <p class="result-def">${esc(r.shortDef)}</p>
            </div>
        `).join('');
        list.querySelectorAll('.result-item').forEach(el =>
            el.addEventListener('click', () => openWord(el.dataset.word))
        );
    }

    /* ═══════════════════════════════════════════════════════════════════════
       Word — load + render
    ═══════════════════════════════════════════════════════════════════════ */
    async function openWord(word) {
        const data = await invoke('getWord', [word]);
        if (!data || !data.found) { toast('Word not found'); return; }
        invoke('addToHistory', [data.word]);
        if (!state.history.includes(data.word)) state.history.unshift(data.word);
        if (state.view === 'word') {
            // Already on word view — save current word in stack so back restores it
            state.stack.push({ view: 'word', scrollTop: $('word-content').scrollTop || 0, word: state.word });
            state.word = data;
            renderWord(data);
        } else {
            state.word = data;
            navigate('word', { word: data });
        }
    }

    function renderWord(data) {
        if (!data?.found) return;

        // Invalidate any in-progress LLM stream for the previous word
        state.llmGen++;
        state.llmBuf    = '';
        state.llmActive = false;

        updateFavBtn(state.favorites.includes(data.word));

        const hasTabs = data.entries.length > 1;
        const tabsHtml = hasTabs
            ? `<div class="pos-tabs">${data.entries.map((e, i) =>
                `<button class="pos-tab${i === 0 ? ' active' : ''}" data-idx="${i}">${esc(e.pos)}</button>`
              ).join('')}</div>`
            : '';

        $('word-content').innerHTML = `
            <div class="word-hero">
                <div class="word-title-row">
                    <h1 class="word-title">${esc(data.word)}</h1>
                    ${data.phonetic
                        ? `<button class="btn-speak" id="btn-speak" title="Pronounce">
                               <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="18" height="18">
                                   <polygon points="11 5 6 9 2 9 2 15 6 15 11 19 11 5"/>
                                   <path d="M15.54 8.46a5 5 0 0 1 0 7.07M19.07 4.93a10 10 0 0 1 0 14.14"/>
                               </svg>
                           </button>`
                        : ''}
                </div>
                ${data.phonetic ? `<div class="word-phonetic">${esc(data.phonetic)}</div>` : ''}
            </div>

            ${tabsHtml}
            <div id="pos-panel">${posPanelHtml(data.entries[0])}</div>

            <div class="word-section ai-section">
                <button class="ai-header" id="ai-toggle">
                    <span class="ai-title">Ask Lingua AI</span>
                    <span id="ai-badge" class="ai-badge ${state.llmReady ? 'badge-ready' : state.llmLoading ? 'badge-loading' : 'badge-error'}">
                        ${state.llmReady ? 'ready' : state.llmLoading ? 'loading…' : 'unavailable'}
                    </span>
                    <svg id="ai-chevron" class="ai-chevron" viewBox="0 0 24 24" fill="none"
                         stroke="currentColor" stroke-width="2.5" width="15" height="15">
                        <polyline points="6 9 12 15 18 9"/>
                    </svg>
                </button>
                <div id="ai-body" class="ai-body">
                    <div class="ai-quick">
                        <button class="btn-quick" data-p="explore">Tell me more</button>
                        <button class="btn-quick" data-p="etymology">Etymology</button>
                        <button class="btn-quick" data-p="usage">Usage tips</button>
                    </div>
                    <div class="ai-input-row">
                        <input id="ai-input" class="ai-input" type="text"
                            placeholder="${state.llmReady ? 'Ask anything about this word…' : state.llmLoading ? 'AI is loading…' : 'No AI model found'}" spellcheck="false"
                            ${state.llmReady ? '' : 'disabled'}>
                        <button id="btn-ask" class="btn-ask" ${state.llmReady ? '' : 'disabled'}>Ask</button>
                    </div>
                    <div id="ai-response" class="ai-response"></div>
                </div>
            </div>
        `;

        // Chip navigation — delegation covers chips re-rendered on tab switch
        $('word-content').addEventListener('click', e => {
            const chip = e.target.closest('.chip[data-word]');
            if (chip) openWord(chip.dataset.word);
        });

        // Speak button
        const speakBtn = $('btn-speak');
        if (speakBtn) speakBtn.addEventListener('click', () => invoke('speakWord', [data.word]));

        // POS tabs
        if (hasTabs) {
            $('word-content').querySelectorAll('.pos-tab').forEach(tab => {
                tab.addEventListener('click', () => {
                    $('word-content').querySelectorAll('.pos-tab').forEach(t => t.classList.remove('active'));
                    tab.classList.add('active');
                    $('pos-panel').innerHTML = posPanelHtml(data.entries[+tab.dataset.idx]);
                });
            });
        }

        // AI section toggle
        let aiOpen = true;
        $('ai-toggle').addEventListener('click', () => {
            aiOpen = !aiOpen;
            $('ai-body').style.display = aiOpen ? '' : 'none';
            $('ai-chevron').style.transform = aiOpen ? '' : 'rotate(-90deg)';
        });

        // Quick prompts
        $('word-content').querySelectorAll('.btn-quick').forEach(btn => {
            btn.addEventListener('click', () => {
                const prompts = {
                    explore:   `Tell me something fascinating and insightful about the word "${data.word}".`,
                    etymology: `What is the etymology and historical origin of the word "${data.word}"?`,
                    usage:     `How should I use "${data.word}" correctly? Give me usage tips and common mistakes.`,
                };
                $('ai-input').value = prompts[btn.dataset.p] || '';
                askLlm(data.word);
            });
        });

        $('btn-ask').addEventListener('click', () => askLlm(data.word));
        $('ai-input').addEventListener('keydown', e => { if (e.key === 'Enter') askLlm(data.word); });

        $('word-content').scrollTop = 0;
    }

    function posPanelHtml(entry) {
        const syns = entry.synonyms || [];
        const ants = entry.antonyms || [];
        const rel  = [...(entry.hypernyms || []), ...(entry.hyponyms || [])].slice(0, 12);
        return `
            <div class="definitions">${entry.definitions.map((d, i) => `
                <div class="def-item">
                    <span class="def-num">${i + 1}</span>
                    <div class="def-body">
                        <p class="def-text">${esc(d.text)}</p>
                        ${d.examples.map(ex => `<p class="def-ex">&ldquo;${esc(ex)}&rdquo;</p>`).join('')}
                    </div>
                </div>
            `).join('')}</div>
            ${syns.length ? `<div class="word-section">
                <h3 class="section-title">Synonyms</h3>
                <div class="chip-row">${chipsHtml(syns, 'chip-syn')}</div>
            </div>` : ''}
            ${ants.length ? `<div class="word-section">
                <h3 class="section-title">Antonyms</h3>
                <div class="chip-row">${chipsHtml(ants, 'chip-ant')}</div>
            </div>` : ''}
            ${rel.length ? `<div class="word-section">
                <h3 class="section-title">Related</h3>
                <div class="chip-row">${chipsHtml(rel, 'chip-rel')}</div>
            </div>` : ''}
        `;
    }

    function chipsHtml(words, cls) {
        return words.map(w =>
            `<button class="chip ${esc(cls)}" data-word="${esc(w)}">${esc(w)}</button>`
        ).join('');
    }

    function dedupe(arr) {
        return [...new Set(arr)];
    }

    function updateFavBtn(isFav) {
        const btn = $('word-fav-btn');
        if (!btn) return;
        const svg = btn.querySelector('svg');
        svg.setAttribute('fill',   isFav ? 'var(--accent)' : 'none');
        svg.setAttribute('stroke', isFav ? 'var(--accent)' : 'currentColor');
        btn.title = isFav ? 'Remove from favorites' : 'Save word';
    }

    /* ═══════════════════════════════════════════════════════════════════════
       Favorites
    ═══════════════════════════════════════════════════════════════════════ */
    function renderFavorites() {
        const list = $('favs-list');
        if (!state.favorites.length) {
            list.innerHTML = '<p class="empty-msg">No saved words yet.<br>Tap ★ on any word to save it here.</p>';
            return;
        }
        list.innerHTML = state.favorites.map(w => `
            <div class="result-item fav-item" data-word="${esc(w)}">
                <div class="result-row">
                    <span class="result-word">${esc(w)}</span>
                    <button class="btn-remove-fav" data-word="${esc(w)}" title="Remove">&times;</button>
                </div>
            </div>
        `).join('');

        list.querySelectorAll('.fav-item').forEach(el => {
            el.addEventListener('click', e => {
                if (!e.target.classList.contains('btn-remove-fav')) openWord(el.dataset.word);
            });
        });
        list.querySelectorAll('.btn-remove-fav').forEach(btn => {
            btn.addEventListener('click', async e => {
                e.stopPropagation();
                const res = await invoke('toggleFavorite', [btn.dataset.word]);
                state.favorites = res.favorites || [];
                renderFavorites();
                toast('Removed from favorites');
            });
        });
    }

    /* ═══════════════════════════════════════════════════════════════════════
       LLM
    ═══════════════════════════════════════════════════════════════════════ */
    async function askLlm(word) {
        if (state.llmActive) return;
        const inputEl = $('ai-input');
        const prompt  = inputEl?.value?.trim();
        if (!prompt) return;
        if (!state.llmReady) { toast('AI is still loading…'); return; }

        const respEl = $('ai-response');
        respEl.textContent = '';
        respEl.classList.add('streaming');
        state.llmBuf    = '';
        state.llmActive = true;

        // Capture generation so stale events from a previous word are ignored
        const myGen = state.llmGen;
        state._tokenHandler = (token, done) => {
            if (state.llmGen !== myGen) { state._tokenHandler = null; return; }
            onLlmToken(token, done);
        };
        state._thinkHandler = (active) => {
            if (state.llmGen !== myGen) { state._thinkHandler = null; return; }
            onLlmThinking(active);
        };

        await invoke('askLlm', [prompt, { word }]);
    }

    function onLlmToken(token, done) {
        if (done) {
            state.llmActive    = false;
            state._tokenHandler = null;
            state._thinkHandler = null;
            $('ai-response')?.classList.remove('streaming');
            return;
        }
        state.llmBuf += token;
        const el = $('ai-response');
        if (el) el.textContent = state.llmBuf;
    }

    function onLlmThinking(active) {
        const el = $('ai-response');
        if (!el) return;
        if (active && !state.llmBuf) {
            el.innerHTML = '<span class="thinking-dot">Thinking…</span>';
        } else {
            el.textContent = state.llmBuf;
        }
    }

    /* ═══════════════════════════════════════════════════════════════════════
       Python → JS events
    ═══════════════════════════════════════════════════════════════════════ */
    function handleEvent(name, data) {
        if (name === 'llmToken') {
            state._tokenHandler?.(data.token, data.done);
        }
        if (name === 'llmThinking') {
            state._thinkHandler?.(data.active);
        }
        if (name === 'wnReady' && data.ready) {
            invoke('getWordOfDay').then(res => {
                state.wotd = res;
                if (state.view === 'home') renderWotd();
            });
        }
        if (name === 'llmReady') {
            state.llmReady   = data.ready;
            state.llmLoading = false;
            const badge  = $('ai-badge');
            const input  = $('ai-input');
            const askBtn = $('btn-ask');
            if (badge) {
                badge.textContent = data.ready ? 'ready' : 'unavailable';
                badge.className   = `ai-badge ${data.ready ? 'badge-ready' : 'badge-error'}`;
            }
            if (input) {
                input.disabled    = !data.ready;
                input.placeholder = data.ready ? 'Ask anything about this word…' : 'No AI model found';
            }
            if (askBtn) askBtn.disabled = !data.ready;
        }
    }

    /* ═══════════════════════════════════════════════════════════════════════
       Bootstrap — poll until the orbit bridge injection has completed,
       then hand off to the main init function.
    ═══════════════════════════════════════════════════════════════════════ */
    function waitForOrbit(fn) {
        if (typeof window.__orbitReady === 'function') {
            window.__orbitReady(fn);
        } else {
            setTimeout(() => waitForOrbit(fn), 25);
        }
    }

    waitForOrbit(async function (orbit) {
        orbit.onEvent(handleEvent);

        // Check LLM status (may have loaded during startup)
        const llmStatus  = await invoke('getLlmStatus');
        state.llmReady   = llmStatus.ready;
        state.llmLoading = llmStatus.loading;

        // Back buttons
        $('results-back').addEventListener('click', goBack);
        $('word-back').addEventListener('click',    goBack);
        $('favs-back').addEventListener('click',    goBack);

        // Home buttons
        $('results-home').addEventListener('click', goHome);
        $('word-home').addEventListener('click',    goHome);

        // Home → Favorites
        $('btn-favs').addEventListener('click', () => navigate('favorites'));

        // Word favorite toggle
        $('word-fav-btn').addEventListener('click', async () => {
            if (!state.word) return;
            const res = await invoke('toggleFavorite', [state.word.word]);
            state.favorites = res.favorites || [];
            updateFavBtn(state.favorites.includes(state.word.word));
            toast(res.action === 'added' ? 'Saved to favorites' : 'Removed from favorites');
        });

        // Search bars
        wireSearch('home-search');
        wireSearch('results-search');
        wireSearch('word-search');

        // Load home data
        await initHome();
    });

})();
