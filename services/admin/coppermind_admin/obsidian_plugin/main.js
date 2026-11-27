/* Coppermind companion: one Review screen. A recording comes in, is summarized and filled in; the owner fixes what is wrong on its card and marks it Reviewed. */
const {Plugin, ItemView, PluginSettingTab, Setting, SecretComponent, Notice, requestUrl, Modal} = require('obsidian');
const VIEW = 'coppermind-review';
const PEOPLE_VIEW = 'coppermind-people';
// Earlier versions had three screens; a saved layout may still hold one.
const RETIRED_VIEWS = ['coppermind-waiting', 'coppermind-notes-in-review'];
const COMPANION_PERMISSIONS = 'Read notes, Add and change notes, Hand in recordings and files, Search, and Read and correct people and companies';
const KEY_MISSING = 'This key is missing something the companion needs. In Coppermind Admin, open Advanced, then Connected apps, and either pair this device with a code, or make a key with: ' + COMPANION_PERMISSIONS + ', then select it in the companion settings.';
const NOT_ANSWERING = 'Coppermind is not answering. Check your connection and that this device is still paired.';
const NOT_SYNCED = 'This note has not reached this device yet.';
const POLL_MS = 15000;
const BUSY_POLL_MS = 3000;
const FILED_SHOWN_MS = 60000;
const MODELS_FRESH_MS = 300000;
const REQUEST_TIMEOUT_MS = 30000;
const DAY = /^(\d{4})-(\d{2})-(\d{2})$/;
// A date with no time ("2026-11-26") is that calendar day where the owner is; new Date() would read it as UTC midnight and show the day before.
const parseDate = value => {const day = DAY.exec(typeof value === 'string' ? value.trim() : ''); return day ? new Date(+day[1], +day[2] - 1, +day[3]) : new Date(value);};
const settle = promise => promise.then(value => ({ok: true, value}), error => ({ok: false, error}));
const plural = (n, one, many) => n + ' ' + (n === 1 ? one : many || one + 's');
// A time as the owner reads it: just the time for today, the day for any other, the day alone when no time was kept.
const when = value => {
  const date = value && parseDate(value); if (!date || isNaN(date)) return '';
  if (DAY.test(String(value).trim())) return date.toLocaleDateString([], {weekday: 'short', month: 'short', day: 'numeric'});
  const today = date.toDateString() === new Date().toDateString();
  return date.toLocaleString([], today ? {timeStyle: 'short'} : {weekday: 'short', hour: 'numeric', minute: '2-digit'});
};
const list = value => (Array.isArray(value) ? value : value ? [value] : []).map(String).filter(Boolean);
const same = (a, b) => JSON.stringify(a ?? null) === JSON.stringify(b ?? null);
// "work/2026/2026-11/Call.md" is shown "work › 2026 › 2026-11".
const where = path => String(path || '').split('/').slice(0, -1).join(' › ');
const link = value => String(value).replace(/^\[\[|\]\]$/g, '').split('|').pop().split('/').pop().replace(/\.md$/, '');

// What a card knows about one note, in one place so the screen and its tests read the same rules.
const rules = {
  // The newest first, as the server lists them; a card filed this session keeps its place until it goes.
  arrange(notes, previous, filed) {
    const byId = new Map(notes.map(n => [n.id, n]));
    const out = [], seen = new Set();
    for (const id of previous) {
      if (byId.has(id)) {out.push({note: byId.get(id)}); seen.add(id);}
      else if (filed[id]) {out.push({filed: filed[id]}); seen.add(id);}
    }
    const fresh = notes.filter(n => !seen.has(n.id));
    return [...fresh.map(note => ({note})), ...out];
  },
  // The words for how a note's summary stands, or nothing when it is fine.
  summaryLine(note) {
    const step = note.writeup || {};
    if (step.state === 'working') return {text: 'Summarizing' + (step.when ? ', started ' + when(step.when) : '') + '.', busy: true};
    // One plain sentence on the card; the server's longer reason stays under the pointer.
    if (step.state === 'failed') return {text: 'The summary did not finish.', detail: step.reason || '', problem: true};
    if (step.state === 'missing' && note.can_resummarize) return {text: 'The summary did not finish.', problem: true};
    return null;
  },
  // Where the owner's choice goes: one value for a single property, a list for the others.
  withValue(values, value, add) {return add ? (values.includes(value) ? values : [...values, value]) : values.filter(v => v !== value);},
};

class ReviewView extends ItemView {
  constructor(leaf, plugin) {
    super(leaf); this.plugin = plugin; this.generation = 0; this.order = []; this.local = {}; this.filed = {}; this.seen = {};
  }
  getViewType() {return VIEW;}
  getDisplayText() {return 'Review';}
  getIcon() {return 'clipboard-check';}
  async onOpen() {
    this.registerEvent?.(this.app.metadataCache?.on?.('changed', file => this.noteChanged(file)));
    this.schedule(); await this.refresh();
  }
  async onClose() {clearTimeout(this.timer); clearTimeout(this.redrawTimer); this.generation++;}
  schedule() {
    clearTimeout(this.timer);
    const busy = (this.data?.notes || []).some(n => n.writeup?.state === 'working') || Object.values(this.local).some(l => l.reviewing) || (this.data?.waiting?.processing || []).length > 0;
    this.timer = setTimeout(async () => {await this.refresh(true); this.schedule();}, busy ? (this.plugin.busyPollMs || BUSY_POLL_MS) : (this.plugin.pollMs || POLL_MS));
  }
  fail(e, fallback) {return e.status === 403 ? KEY_MISSING : e.status === 422 || e.status === 404 ? e.message : fallback;}
  // -- reading ------------------------------------------------------------------------
  async loadNotes() {
    let cursor = '', data = null; const notes = [];
    for (let pages = 0; pages < 100; pages++) {
      const page = await this.plugin.call('/v1/review/notes' + (cursor ? '?cursor=' + encodeURIComponent(cursor) : ''), 'GET', undefined, true);
      data = page; notes.push(...(page.notes || [])); cursor = page.next_cursor || '';
      if (!cursor) break;
    }
    return {...data, notes};
  }
  async refresh(quiet) {
    if (quiet && (this.refreshing || this.busy)) return;
    const generation = ++this.generation, el = this.contentEl; this.refreshing = true;
    if (!this.loaded) {el.empty(); el.createEl('h2', {text: 'Review'}); el.createEl('p', {text: 'Loading…', cls: 'coppermind-hint'});}
    try {
      const [waiting, notes] = await Promise.all([settle(this.plugin.call('/v1/recordings/waiting', 'GET', undefined, true)), settle(this.loadNotes())]);
      if (generation !== this.generation) return;
      if (!waiting.ok && !notes.ok) {
        const problem = waiting.error?.status === 403 ? KEY_MISSING : NOT_ANSWERING;
        if (this.loaded) {this.problem = problem; this.render(); return;}
        el.empty(); el.createEl('h2', {text: 'Review'});
        el.createEl('p', {text: problem, cls: 'coppermind-notice'});
        const retry = el.createEl('button', {text: 'Try again'}); retry.onclick = () => this.refresh();
        return;
      }
      if (notes.ok && notes.value.notes.some(n => n.can_resummarize) && (!this.models || Date.now() - this.modelsAt > MODELS_FRESH_MS)) await this.loadModels();
      if (generation !== this.generation || (quiet && this.busy)) return;
      this.problem = '';
      const data = {
        waiting: waiting.ok ? waiting.value : this.data?.waiting,
        page: notes.ok ? notes.value : this.data?.page,
        notesError: notes.ok ? '' : this.fail(notes.error, NOT_ANSWERING),
      };
      await this.afterRead(data);
      if (generation !== this.generation) return;
      const fingerprint = JSON.stringify(data) + JSON.stringify(this.local) + JSON.stringify(this.filed) + JSON.stringify(this.models);
      if (quiet && this.loaded && fingerprint === this.fingerprint) return;
      this.fingerprint = fingerprint;
      this.data = {...data, notes: data.page?.notes || []};
      this.loaded = true; this.render();
    } finally {if (generation === this.generation) this.refreshing = false;}
  }
  // What changed since the last read: a summary that finished, a note that has been filed.
  async afterRead(data) {
    const notes = data.page?.notes || [], present = new Set(notes.map(n => n.id));
    for (const n of notes) {
      const before = this.seen[n.id], now = n.writeup?.state;
      if (before === 'working' && now === 'written') this.say(n.id, 'Summary rewritten.');
      this.seen[n.id] = now;
    }
    if (!data.page) return;
    await Promise.all(Object.entries(this.local).filter(([id, mine]) => mine.reviewing && !present.has(id)).map(([id]) => this.markFiled(id)));
  }
  async markFiled(id) {
    const mine = this.local[id]; if (!mine || mine.filing) return; mine.filing = true;
    let path = '';
    try {path = (await this.plugin.call('/v1/notes/' + encodeURIComponent(id))).path || '';} catch {}
    mine.reviewing = false; mine.filing = false;
    this.filed[id] = {id, title: mine.title, folder: where(path)};
    setTimeout(() => {delete this.filed[id]; this.render();}, this.plugin.filedShownMs || FILED_SHOWN_MS);
  }
  noteChanged(file) {
    if (!this.loaded || !(this.data?.notes || []).some(n => n.path === file.path)) return;
    clearTimeout(this.redrawTimer);
    this.redrawTimer = setTimeout(() => {if (!this.typing) this.render();}, 300);
  }
  // -- the note's properties, straight from the vault ----------------------------------
  keys() {return this.data?.page?.choices?.keys || {};}
  choices() {return this.data?.page?.choices || {};}
  fileOf(note) {return this.app.vault.getAbstractFileByPath(note.path);}
  props(note) {
    const keys = this.keys(), file = this.fileOf(note);
    const cache = (file && this.app.metadataCache.getFileCache(file)?.frontmatter) || {};
    const mine = this.local[note.id] ??= {};
    mine.props ??= {};
    for (const [key, value] of Object.entries(mine.props)) if (file && same(cache[key], value)) delete mine.props[key];
    const get = name => name in mine.props ? mine.props[name] : cache[name];
    return {
      synced: !!file,
      type: get(keys.type) ?? '',
      account: list(get(keys.account)),
      tags: list(get(keys.tags)),
      meeting: get(keys.meeting) ?? note.meeting?.selected ?? '',
      who: [...new Set(list(get(keys.attendees)).map(link))],
      reviewed: get(keys.reviewed) === true,
    };
  }
  async writeProp(note, key, value) {
    const file = this.fileOf(note);
    if (!file) {this.say(note.id, NOT_SYNCED, true); this.redraw(); return false;}
    try {
      await this.app.fileManager.processFrontMatter(file, fm => {fm[key] = value;});
    } catch (e) {this.say(note.id, 'Could not change this note. Try again.', true); this.redraw(); return false;}
    ((this.local[note.id] ??= {}).props ??= {})[key] = value;
    this.say(note.id, '');
    this.redraw();
    return true;
  }
  say(id, text, problem) {this.local[id] = {...this.local[id], message: text, problem: !!problem};}
  // -- drawing ------------------------------------------------------------------------
  render() {
    const el = this.contentEl, data = this.data; if (!data) return;
    el.empty(); this.busy = false;
    const waiting = data.waiting || {}, failed = waiting.failed || [], processing = waiting.processing || [];
    const items = rules.arrange(data.notes, this.order, this.filed);
    this.order = items.map(i => i.note?.id || i.filed.id);
    const count = items.filter(i => i.note).length;
    el.createEl('h2', {text: 'Review'});
    if (count) el.createEl('p', {text: count + ' waiting', cls: 'coppermind-count'});
    if (this.problem) this.trouble(this.problem);
    if (data.notesError) this.trouble(data.notesError);
    for (const recording of failed) this.failedRow(recording);
    for (const item of processing) this.progressRow(item);
    if (!items.length && !failed.length && !processing.length && !this.problem && !data.notesError) el.createEl('p', {text: 'Nothing to review.', cls: 'coppermind-empty'});
    for (const item of items) item.note ? this.card(item.note) : this.filedRow(item.filed);
    this.schedule();
  }
  trouble(text) {
    const row = this.contentEl.createDiv({cls: 'coppermind-card coppermind-trouble'});
    row.createEl('p', {text});
    const again = row.createEl('button', {text: 'Try again'}); again.onclick = () => this.refresh();
  }
  failedRow(recording) {
    const row = this.contentEl.createDiv({cls: 'coppermind-card coppermind-trouble'});
    const head = row.createDiv({cls: 'coppermind-head'}); head.createEl('strong', {text: recording.title, cls: 'coppermind-name'});
    row.createEl('p', {text: recording.message});
    const bar = row.createDiv({cls: 'coppermind-actions'}), line = row.createEl('p', {cls: 'coppermind-notice'});
    if (recording.can_retry) {
      const again = bar.createEl('button', {text: 'Try again'});
      again.onclick = async () => {again.disabled = true; line.setText('Trying again…');
        try {await this.plugin.call('/v1/recordings/' + encodeURIComponent(recording.id) + '/retry', 'POST', {}); await this.refresh();}
        catch (e) {line.setText(this.fail(e, 'Could not try again. Check your connection.')); again.disabled = false;}
      };
    }
    if (recording.can_dismiss) {
      const dismiss = bar.createEl('button', {text: 'Dismiss'});
      dismiss.onclick = async () => {dismiss.disabled = true;
        try {await this.plugin.call('/v1/recordings/' + encodeURIComponent(recording.id) + '/dismiss', 'POST', {}); await this.refresh();}
        catch (e) {line.setText(this.fail(e, 'Could not dismiss this. Check your connection.')); dismiss.disabled = false;}
      };
    }
  }
  progressRow(item) {
    const row = this.contentEl.createDiv({cls: 'coppermind-card coppermind-quiet'});
    const head = row.createDiv({cls: 'coppermind-head'}); head.createEl('strong', {text: item.title, cls: 'coppermind-name'});
    row.createEl('p', {text: item.message, cls: 'coppermind-hint'});
  }
  filedRow(filed) {
    const row = this.contentEl.createDiv({cls: 'coppermind-card coppermind-filed'});
    const head = row.createDiv({cls: 'coppermind-head'});
    head.createEl('strong', {text: filed.title, cls: 'coppermind-name'});
    row.createEl('p', {text: 'Filed ✓' + (filed.folder ? ' ' + filed.folder : ''), cls: 'coppermind-ok'});
    const bar = row.createDiv({cls: 'coppermind-actions'}), line = row.createEl('p', {cls: 'coppermind-notice'});
    const undo = bar.createEl('button', {text: 'Undo'});
    undo.onclick = async () => {undo.disabled = true;
      try {await this.plugin.call('/v1/notes/' + encodeURIComponent(filed.id) + '/reopen', 'POST', {}); delete this.filed[filed.id]; delete this.local[filed.id]; await this.refresh();}
      catch (e) {line.setText(this.fail(e, 'Could not undo this. Check your connection.')); undo.disabled = false;}
    };
  }
  redraw() {this.render();}
  card(note) {
    const card = this.contentEl.createDiv({cls: 'coppermind-card coppermind-note'});
    const mine = this.local[note.id] ??= {}; mine.title = note.title;
    const props = this.props(note);
    const head = card.createDiv({cls: 'coppermind-head'});
    const open = head.createEl('button', {text: note.title, cls: 'coppermind-title'});
    open.onclick = async () => {
      const file = this.fileOf(note);
      if (!file) {new Notice(NOT_SYNCED); return;}
      await this.app.workspace.getLeaf(false).openFile(file);
    };
    const shown = when(note.date); if (shown) head.createEl('span', {text: shown, cls: 'coppermind-hint'});
    if (note.filing?.problem) {this.filingProblem(card, note); return;}
    if (mine.reviewing) {this.reviewingLine(card, note, props); return;}
    // The note is on the server and has not reached this device; its properties cannot be shown or changed yet.
    if (!props.synced) {card.createEl('p', {text: 'Waiting to sync.', cls: 'coppermind-hint'}); return;}
    const fields = card.createDiv({cls: 'coppermind-fields'});
    if (note.meeting?.candidates?.length) this.meetingField(fields, note, props);
    this.typeField(fields, note, props);
    this.pillsField(fields, note, props, 'Account', this.keys().account, props.account, this.choices().accounts || [], false);
    this.pillsField(fields, note, props, 'Tags', this.keys().tags, props.tags, this.choices().tags || [], this.choices().tags_closed);
    // Piece 4 puts one voice picker per voice here. Until then voices are named in the note itself, and this shows who was matched.
    const who = fields.createDiv({cls: 'coppermind-who'});
    if (props.who.length) {who.createEl('label', {text: 'Who spoke'}); who.createSpan({text: props.who.join(', ')});}
    const summary = rules.summaryLine(note);
    if (summary) card.createEl('p', {text: summary.text, attr: summary.detail ? {title: summary.detail} : undefined, cls: summary.problem ? 'coppermind-notice coppermind-problem' : 'coppermind-hint'});
    const message = mine.message;
    const line = card.createEl('p', {text: message || '', cls: message ? (mine.problem ? 'coppermind-notice coppermind-problem' : 'coppermind-notice') : ''});
    this.actions(card, note, props, summary, line);
  }
  field(parent, label) {
    const row = parent.createDiv({cls: 'coppermind-field'});
    row.createEl('label', {text: label});
    return row.createDiv({cls: 'coppermind-control'});
  }
  meetingField(parent, note, props) {
    const control = this.field(parent, 'Meeting');
    const select = control.createEl('select');
    select.createEl('option', {text: 'No meeting', value: ''});
    for (const event of note.meeting.candidates) {
      const at = when(event.start);
      select.createEl('option', {text: event.title + (at ? ' · ' + at : ''), value: event.event_id});
    }
    select.value = props.meeting;
    select.onchange = async () => {
      if (!select.value) return;
      select.disabled = true;
      try {
        await this.plugin.call('/v1/notes/' + encodeURIComponent(note.id) + '/meeting', 'POST', {event_id: select.value});
        ((this.local[note.id] ??= {}).props ??= {})[this.keys().meeting] = select.value; this.say(note.id, '');
        await this.refresh();
      } catch (e) {this.say(note.id, this.fail(e, 'Could not change the meeting. Check your connection.'), true); this.render();}
    };
  }
  typeField(parent, note, props) {
    const types = this.choices().types || [], key = this.keys().type;
    if (!key || !types.length) return;
    const control = this.field(parent, 'Type');
    if (types.length > 4) {
      const select = control.createEl('select');
      for (const type of ['', ...types]) select.createEl('option', {text: type || 'None', value: type});
      select.value = props.type; select.onchange = () => this.writeProp(note, key, select.value);
      return;
    }
    const group = control.createDiv({cls: 'coppermind-choice'});
    for (const type of types) {
      const button = group.createEl('button', {text: type.charAt(0).toUpperCase() + type.slice(1), cls: type === props.type ? 'coppermind-chosen mod-cta' : ''});
      button.onclick = () => this.writeProp(note, key, type);
    }
  }
  pillsField(parent, note, props, label, key, values, options, closed) {
    if (!key) return;
    const control = this.field(parent, label);
    const pills = control.createDiv({cls: 'coppermind-pills'});
    for (const value of values) {
      const pill = pills.createEl('span', {cls: 'coppermind-pill'}); pill.createSpan({text: value});
      const remove = pill.createEl('button', {text: '×', cls: 'coppermind-remove', attr: {'aria-label': 'Remove ' + value}});
      remove.onclick = () => this.writeProp(note, key, rules.withValue(values, value, false));
    }
    const id = 'coppermind-' + label.toLowerCase() + '-' + note.id;
    const add = pills.createEl('input', {attr: {type: 'text', placeholder: '+ add', list: id}, cls: 'coppermind-add'});
    const datalist = pills.createEl('datalist', {attr: {id}});
    for (const option of options) if (!values.includes(option)) datalist.createEl('option', {value: option});
    add.onfocus = () => {this.typing = true;}; add.onblur = () => {this.typing = false;};
    add.onchange = async () => {
      let value = add.value.trim(); this.typing = false; if (!value) return;
      const known = options.find(o => o.toLowerCase() === value.toLowerCase());
      if (known) value = known;
      else if (closed) {this.say(note.id, '“' + value + '” is not one of your tags.', true); this.render(); return;}
      await this.writeProp(note, key, rules.withValue(values, value, true));
    };
  }
  filingProblem(card, note) {
    card.createEl('p', {text: note.filing.problem, cls: 'coppermind-notice coppermind-problem'});
    const bar = card.createDiv({cls: 'coppermind-actions'}), line = card.createEl('p', {cls: 'coppermind-notice'});
    const again = bar.createEl('button', {text: 'Try again'});
    again.onclick = async () => {
      again.disabled = true; line.setText('Trying again…');
      try {const result = await this.plugin.call('/v1/notes/' + encodeURIComponent(note.id) + '/file-again', 'POST', {}); line.setText(result.filed ? 'Filed.' : result.problem || ''); await this.refresh();}
      catch (e) {again.disabled = false; line.setText(this.fail(e, 'Could not try again. Check your connection.'));}
    };
  }
  reviewingLine(card, note, props) {
    card.createEl('p', {text: 'Reviewed. Filing…', cls: 'coppermind-hint'});
    const bar = card.createDiv({cls: 'coppermind-actions'});
    const undo = bar.createEl('button', {text: 'Undo'});
    undo.onclick = async () => {
      undo.disabled = true;
      if (await this.writeProp(note, this.keys().reviewed, false)) {this.local[note.id].reviewing = false; this.render();}
      else undo.disabled = false;
    };
  }
  // -- the two buttons ----------------------------------------------------------------
  actions(card, note, props, summary, line) {
    const bar = card.createDiv({cls: 'coppermind-actions'});
    const confirm = card.createDiv({cls: 'coppermind-confirm'});
    if (note.can_resummarize) {
      const working = !!summary?.busy;
      let model = '';
      const models = this.models || [];
      if (models.length > 1) {
        const select = bar.createEl('select', {attr: {'aria-label': 'Model', title: 'The model for this one Resummarize'}, cls: 'coppermind-model'});
        for (const name of models) select.createEl('option', {text: name, value: name});
        select.value = this.defaultModel; model = select.value; select.onchange = () => {model = select.value;};
      }
      const button = bar.createEl('button', {text: working ? 'Resummarizing…' : 'Resummarize'}); button.disabled = working;
      const start = async overwrite => {
        button.disabled = true; button.setText('Resummarizing…'); line.setText('');
        const body = {}; if (model && model !== this.defaultModel) body.model = model; if (overwrite) body.overwrite = true;
        try {await this.plugin.call('/v1/notes/' + encodeURIComponent(note.id) + '/resummarize', 'POST', body); this.say(note.id, ''); await this.refresh();}
        catch (e) {button.disabled = false; button.setText('Resummarize'); this.busy = false; this.say(note.id, this.fail(e, 'Could not start. Check your connection.'), true); this.render();}
      };
      button.onclick = () => {
        if (!note.edited) return start(false);
        this.busy = true; button.disabled = true; confirm.empty();
        confirm.createEl('p', {text: 'Replace your edited note with the new summary? Your version stays in history.'});
        const yes = confirm.createEl('button', {text: 'Replace', cls: 'mod-warning'}), no = confirm.createEl('button', {text: 'Cancel'});
        yes.onclick = () => {this.busy = false; confirm.empty(); return start(true);};
        no.onclick = () => {this.busy = false; confirm.empty(); button.disabled = false;};
      };
    }
    const done = bar.createEl('button', {text: 'Reviewed', cls: 'mod-cta'});
    done.onclick = async () => {
      done.disabled = true;
      if (await this.writeProp(note, this.keys().reviewed, true)) {(this.local[note.id] ??= {}).reviewing = true; this.render(); this.refresh(true);}
      else done.disabled = false;
    };
  }
  // The model choice is what the connection reports; the configured one starts selected.
  async loadModels() {
    try {
      const result = await this.plugin.call('/v1/review/models', 'GET', undefined, true);
      this.models = result.models || []; this.defaultModel = result.default || this.models[0] || '';
    } catch {this.models = []; this.defaultModel = '';}
    this.modelsAt = Date.now();
  }
}
class DeleteVoiceModal extends Modal {
  constructor(app, person, confirm, options={}){super(app);this.person=person;this.confirm=confirm;this.options=options;this.done=false;}
  onOpen(){
    const o=this.options;
    this.contentEl.createEl('h2',{text:o.title||'Delete voice signatures?'});
    this.contentEl.createEl('p',{text:o.text||'All saved voice signatures for '+this.person.name+' will be deleted. This cannot be undone.'});
    const button=this.contentEl.createEl('button',{text:o.action||'Delete signatures',cls:'mod-warning'});
    button.onclick=async()=>{this.done=true;this.close();await this.confirm();};
    const cancel=this.contentEl.createEl('button',{text:'Cancel'});cancel.onclick=()=>this.close();
  }
  onClose(){this.contentEl.empty();if(!this.done&&this.options.cancel)this.options.cancel();}
}
class PeopleView extends ItemView {
  constructor(leaf,plugin){super(leaf);this.plugin=plugin;}
  getViewType(){return PEOPLE_VIEW;}
  getDisplayText(){return 'People';}
  getIcon(){return 'users';}
  async onOpen(){await this.show();}
  async show(person){
    const el=this.contentEl;el.empty();el.createEl('h2',{text:'People and voices'});
    try{
      const recognition=await this.plugin.call('/v1/entities/voice/recognition');
      new Setting(el).setName('Voice recognition').setDesc('Store voice signatures to recognise speakers in future recordings. No audio clips are saved for recognition.').addToggle(t=>t.setValue(recognition.enabled).onChange(async enabled=>{try{await this.plugin.call('/v1/entities/voice/recognition','PUT',{enabled});await this.show(person);}catch(e){new Notice(e.message);}}));
      if(!recognition.enabled)el.createEl('p',{text:'New voice signatures are not being stored. Existing voice data is listed below.'});
      const unknowns=await this.plugin.call('/v1/entities/voice/unknowns');
      const savedPeople=await this.plugin.call('/v1/entities/voice/people');
      const search=el.createEl('input',{attr:{type:'search',placeholder:'Find a person'}});
      const find=el.createEl('button',{text:'Search'});const list=el.createDiv();
      find.onclick=async()=>{try{list.empty();const page=await this.plugin.call('/v1/entities?kind=person&limit=50&q='+encodeURIComponent(search.value.trim()));
        if(!page.items.length)list.createEl('p',{text:'No people found.'});
        for(const item of page.items){const b=list.createEl('button',{text:item.name});b.onclick=()=>this.show(item);}
      }catch(e){new Notice(e.message);}};
      el.createEl('h3',{text:'Saved voices'});
      if(!Object.keys(unknowns).length&&!Object.keys(savedPeople).length)el.createEl('p',{text:'No voices saved yet.'});
      for(const [id,item] of Object.entries(savedPeople)){const b=el.createEl('button',{text:item.name+' · Voice data saved'});b.onclick=()=>this.show({id,name:item.name});}
      for(const [id,item] of Object.entries(unknowns)){const b=el.createEl('button',{text:item.name+' · '+item.assignments.length+' recordings'});b.onclick=()=>this.show({id,name:item.name,unknown:true});}
      if(!person)return;
      const path=person.unknown?'/v1/entities/voice/unknowns/'+encodeURIComponent(person.id):'/v1/entities/'+encodeURIComponent(person.id)+'/voice';
      const voice=person.unknown?unknowns[person.id]:await this.plugin.call(path);
      if(!voice){await this.show();return;}
      el.createEl('h3',{text:person.name});
      el.createEl('p',{text:voice.signatures.length+' saved voice signatures'});
      const attended=new Set();const attendeesKey=recognition.attendees_field||'';
      for(const assignment of voice.assignments||[]){const line=el.createDiv();
        try{const note=await this.plugin.call('/v1/notes/'+encodeURIComponent(assignment.note_id));for(const v of attendeesKey&&Array.isArray(note.frontmatter?.[attendeesKey])?note.frontmatter[attendeesKey]:[])if(typeof v==='string')attended.add(v.trim().replace(/^\[\[|\]\]$/g,'').split('|')[0].replace(/\.md$/,'').toLowerCase());const a=line.createEl('a',{text:note.title,href:'#'});a.onclick=e=>{e.preventDefault();this.app.workspace.openLinkText(note.path,'',false);};}
        catch{line.createSpan({text:'Source note unavailable'});}
        const split=line.createEl('button',{text:'This is not '+person.name});split.onclick=()=>new DeleteVoiceModal(this.app,person,async()=>{try{await this.plugin.call('/v1/entities/voice/voices/'+encodeURIComponent(person.unknown?person.id:'person:'+person.id)+'/split','POST',{note_id:assignment.note_id,label:assignment.label,source_id:assignment.source_id});await this.show(person);}catch(e){new Notice(e.message);}},{title:'Separate this recording?',text:'This recording will use a new unknown voice label. Other recordings stay with '+person.name+'. Your own writing in reviewed notes stays intact.',action:'Separate recording'}).open();
      }
      if(person.unknown){
        el.createEl('p',{text:'Combine this voice with another unknown voice or attach it to a person. Earlier recordings will be relabelled; your own writing in reviewed notes stays intact.'});
        const target=el.createEl('select');target.createEl('option',{text:'Choose a voice or person',value:''});const labels={};let personOptions=[];
        for(const [id,item] of Object.entries(unknowns))if(id!==person.id){target.createEl('option',{text:item.name,value:id});labels[id]=item.name;}
        const query=el.createEl('input',{attr:{type:'search',placeholder:'Type a name to find a person'}});const lookup=el.createEl('button',{text:'Find a person'});
        const found=el.createEl('p');const matches=el.createDiv();const summary=el.createEl('p',{cls:'coppermind-notice'});
        const count=new Set((voice.assignments||[]).map(a=>a.note_id)).size;const recordings=count+' recording'+(count===1?'':'s');
        const describe=()=>{const label=labels[target.value]||'';
          summary.setText(target.value&&label?person.name+' will become '+label+' in '+recordings+'.':'Choose who '+person.name+' should be combined with.');};
        target.onchange=describe;describe();
        const personLabel=(p,all)=>{const email=p.emails?.[0]||'';const clash=all.filter(x=>x.name===p.name).length>1;return p.name+(email?' ('+email+')':clash?' (no email, ID '+String(p.id).slice(0,8)+')':'');};
        let lookupId=0;const attendedBefore=p=>[p.path&&String(p.path).replace(/\.md$/,''),p.name,...(p.emails||[])].some(k=>k&&attended.has(String(k).toLowerCase()));
        const clearPeople=()=>{matches.empty();for(const o of personOptions)o.remove();personOptions=[];for(const k of Object.keys(labels))if(k.startsWith('person:'))delete labels[k];};
        lookup.onclick=async()=>{const mine=++lookupId;try{
          const text=query.value.trim();if(!text){clearPeople();target.value='';found.setText('Type a name first, then choose Find a person.');describe();return;}
          const page=await this.plugin.call('/v1/entities?kind=person&limit=50&q='+encodeURIComponent(text));if(mine!==lookupId)return;const items=page.items||[];
          clearPeople();
          const seen=new Set();const unique=items.filter(p=>seen.has(p.id)?false:(seen.add(p.id),true)).map(p=>({p,here:attendedBefore(p)})).sort((a,b)=>b.here-a.here).map(x=>x.p);
          for(const p of unique){const text2=personLabel(p,unique);personOptions.push(target.createEl('option',{text:text2,value:'person:'+p.id}));labels['person:'+p.id]=text2;
            const pick=matches.createEl('button',{text:text2+(attendedBefore(p)?' · attended these meetings':'')});pick.onclick=()=>{target.value='person:'+p.id;describe();};}
          if(!unique.length){found.setText('No one named '+text+' found.');target.value='';}
          else{found.setText(unique.length===1?'Found 1 person. Check the summary below, then combine.':'Found '+unique.length+' people. Choose one from the list below.');if(unique.length===1)target.value='person:'+unique[0].id;}
          describe();
        }catch(e){if(mine===lookupId){found.setText('Could not search for people.');new Notice(e.message);}}};
        let typing;query.oninput=()=>{clearTimeout(typing);++lookupId;if(!query.value.trim()){lookup.onclick();return;}clearPeople();target.value='';describe();found.setText('Searching…');typing=setTimeout(()=>lookup.onclick(),250);};
        const combine=el.createEl('button',{text:'Choose and combine',cls:'mod-cta'});combine.onclick=()=>{if(!target.value){found.setText('Choose a voice or person first.');return;}const chosen=target.value;const label=labels[chosen]||'the chosen voice';new DeleteVoiceModal(this.app,person,async()=>{try{await this.plugin.call(path+'/combine','POST',{target:chosen});await this.show();el.createEl('p',{text:'Done: '+person.name+' is now '+label+' in '+recordings+'.',cls:'coppermind-notice'});new Notice(person.name+' is now '+label+'.');}catch(e){new Notice(e.message);}},{title:'Combine these voices?',text:person.name+' will become '+label+' in '+recordings+'. Transcripts and unchanged generated labels will change; your own writing in reviewed notes stays intact.',action:'Combine voices'}).open();};
      }
      if(person.unknown||voice.signatures.length){const del=el.createEl('button',{text:'Delete voice data',cls:'mod-warning'});del.onclick=()=>new DeleteVoiceModal(this.app,person,async()=>{try{await this.plugin.call(path,'DELETE');await this.show(person.unknown?undefined:person);}catch(e){new Notice(e.message);}},{title:'Delete voice data?',text:'Delete '+voice.signatures.length+' saved voice signature'+(voice.signatures.length===1?'':'s')+' for '+person.name+'? Future recordings cannot be recognised from these signatures. This cannot be undone.',action:'Delete voice data'}).open();}
    }catch(e){el.createEl('p',{text:e.message,cls:'coppermind-notice'});}
  }
}

const NOT_COPPERMIND='That address did not answer like Coppermind. Check the address and try again.';
function readJson(result){try{const value=result.json;return value&&typeof value==='object'?value:null;}catch{return null;}}
function withDetails(message,result,json){
  const raw=json?null:String(result.text??'').replace(/\s+/g,' ').trim().slice(0,200);
  return Object.assign(Error(message),{details:'HTTP '+result.status+(raw?': '+raw:'')});
}
function showError(el,status,e){
  status.setText(e.message);
  if(e.details){const more=el.createEl('details');more.createEl('summary',{text:'Details for support'});more.createEl('p',{text:e.details});}
}
const PAIR_SECRET='coppermind-companion';
const PAIR_HELP='In Coppermind Admin, open "Add the companion to Obsidian". It shows the six-digit code to type here.';
class PairModal extends Modal {
  constructor(app,plugin,done){super(app);this.plugin=plugin;this.done=done;}
  onOpen(){
    const el=this.contentEl;el.empty();el.createEl('h2',{text:'Pair with Coppermind'});
    el.createEl('p',{text:PAIR_HELP});
    let address=null;
    if(!this.plugin.settings.url){el.createEl('label',{text:'Address of your Coppermind server'});address=el.createEl('input',{attr:{type:'url',placeholder:'https://coppermind.example'}});}
    el.createEl('label',{text:'Pairing code'});
    const code=el.createEl('input',{attr:{type:'text',inputmode:'numeric',autocomplete:'one-time-code',placeholder:'123 456'}});
    const status=el.createEl('p',{cls:'coppermind-notice'});
    const button=el.createEl('button',{text:'Pair this device',cls:'mod-cta'});
    button.onclick=async()=>{
      button.disabled=true;status.setText('Pairing…');
      try{
        if(address){this.plugin.settings.url=address.value.trim().replace(/\/$/,'');await this.plugin.saveData(this.plugin.settings);}
        const name=await this.plugin.pair(code.value);
        status.setText('Paired. Coppermind knows this device as "'+name+'".');
        new Notice('Coppermind is paired with this device.');
        if(this.done)this.done();
        this.close();
      }catch(e){showError(el,status,e);button.disabled=false;}
    };
  }
  onClose(){this.contentEl.empty();}
}
class Settings extends PluginSettingTab {
  constructor(app,plugin){super(app,plugin);this.plugin=plugin;}
  display(){const el=this.containerEl;el.empty();el.createEl('h2',{text:'Coppermind'});
    new Setting(el).setName('Server address').setDesc('The address of your Coppermind server. Filled in by Coppermind when it put this add-on in your vault.').addText(t=>t.setValue(this.plugin.settings.url).onChange(async v=>{this.plugin.settings.url=v.trim().replace(/\/$/,'');await this.plugin.saveData(this.plugin.settings);}));
    new Setting(el).setName('This device').setDesc(this.plugin.paired()?'Paired with Coppermind. To pair again, unpair this device in Coppermind Admin first, then press Pair.':'Not paired yet. '+PAIR_HELP).addButton(b=>b.setButtonText(this.plugin.paired()?'Pair again':'Pair this device').setCta().onClick(()=>new PairModal(this.app,this.plugin,()=>this.display()).open()));
    new Setting(el).setName('Use a key instead').setDesc('Only if you would rather paste a key than pair with a code. In Coppermind Admin, open Connected apps, choose Connect an app, then Something else, and create a new key with: '+COMPANION_PERMISSIONS+'. Select that key here. Only the name of the saved key is kept in these settings.').addComponent(container=>new SecretComponent(this.app,container).setValue(this.plugin.settings.secret).onChange(async v=>{this.plugin.settings.secret=v;await this.plugin.saveData(this.plugin.settings);}));
  }
}

module.exports=class CoppermindReview extends Plugin {
  static get rules(){return rules;}
  async onload(){
    this.settings={url:'',secret:'',...await this.loadData()};
    this.registerView(VIEW,leaf=>new ReviewView(leaf,this));
    this.registerView(PEOPLE_VIEW,leaf=>new PeopleView(leaf,this));
    this.addSettingTab(new Settings(this.app,this));
    // Let go of any copy of a retired screen that a saved layout still holds.
    for(const type of RETIRED_VIEWS)this.app.workspace.detachLeavesOfType?.(type);
    this.app.workspace.detachLeavesOfType?.('coppermind-imports');
    this.addCommand({id:'review',name:'Review',callback:()=>this.openReview()});
    this.addRibbonIcon('clipboard-check','Review',()=>this.openReview());
    this.addCommand({id:'manage-people',name:'Manage people and voice recognition',callback:()=>this.openPeople()});
    this.addCommand({id:'pair-device',name:'Pair this device with Coppermind',callback:()=>new PairModal(this.app,this).open()});
    // A device that has the add-on but no key yet is asked for the pairing code once it is ready.
    this.app.workspace.onLayoutReady?.(()=>{if(!this.paired())new PairModal(this.app,this).open();});
  }
  paired(){try{return !!(this.settings.secret&&this.app.secretStorage?.getSecret(this.settings.secret));}catch{return false;}}
  deviceName(){
    // The name Obsidian Sync shows for this device, so Admin lists the same name twice over.
    try{const sync=this.app.internalPlugins?.plugins?.sync?.instance;const name=sync?.deviceName||sync?.getDefaultDeviceName?.();if(name)return String(name).slice(0,60);}catch{}
    const agent=typeof navigator==='undefined'?'':navigator.userAgent||'';
    if(/iPhone/.test(agent))return 'iPhone';if(/iPad/.test(agent))return 'iPad';if(/Android/.test(agent))return 'Android device';
    if(/Mac/.test(agent))return 'Mac';if(/Windows/.test(agent))return 'Windows computer';if(/Linux/.test(agent))return 'Linux computer';
    return 'a device';
  }
  async pair(code){
    let url;try{url=new URL(this.settings.url);}catch{throw Error('Type the address of your Coppermind server first.');}
    if(!['http:','https:'].includes(url.protocol)||url.username||url.password)throw Error('Type the address of your Coppermind server first.');
    const digits=String(code||'').replace(/\D/g,'');
    if(digits.length!==6)throw Error('Type the six-digit pairing code shown in Coppermind Admin.');
    const result=await requestUrl({url:this.settings.url+'/v1/pair',method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({code:digits,device:this.deviceName()}),throw:false});
    const json=readJson(result);
    if(result.status>=400||!json?.credential)throw withDetails(json?.message||NOT_COPPERMIND,result,json);
    this.app.secretStorage.setSecret(PAIR_SECRET,json.credential);
    this.settings.secret=PAIR_SECRET;await this.saveData(this.settings);
    return json.name;
  }
  async openReview(){
    let leaf=this.app.workspace.getLeavesOfType(VIEW)[0]||this.app.workspace.getRightLeaf(false);
    await leaf.setViewState({type:VIEW,active:true});await this.app.workspace.revealLeaf(leaf);await leaf.view.refresh();
  }
  async openPeople(){
    const file=this.app.workspace.getActiveFile();
    let leaf=this.app.workspace.getLeavesOfType(PEOPLE_VIEW)[0]||this.app.workspace.getRightLeaf(false);
    await leaf.setViewState({type:PEOPLE_VIEW,active:true});await this.app.workspace.revealLeaf(leaf);
    if(file){try{const result=await this.call('/v1/entities?kind=person&limit=50&q='+encodeURIComponent(file.basename));const person=result.items?.find(p=>p.path===file.path);if(person)await leaf.view.show(person);}catch{}}
  }
  // Reads that a screen waits on give up on a silent server. Writes are never cut off, because the server may still be working on them.
  withTimeout(request){
    if(typeof setTimeout!=='function')return request;
    let timer;
    const late=new Promise((_,reject)=>{timer=setTimeout(()=>reject(Object.assign(Error('Coppermind did not answer in time.'),{timeout:true})),this.requestTimeoutMs||REQUEST_TIMEOUT_MS);});
    return Promise.race([request,late]).finally(()=>clearTimeout(timer));
  }
  async call(path,method='GET',body,timed=false){
    const url=new URL(this.settings.url);if(!['http:','https:'].includes(url.protocol)||url.username||url.password)throw Error('Configure a valid Coppermind HTTP(S) address.');
    const key=this.app.secretStorage?.getSecret(this.settings.secret);if(!key)throw Error('This device is not paired with Coppermind yet. '+PAIR_HELP);
    const request=requestUrl({url:this.settings.url+path,method,headers:{Authorization:'Bearer '+key,'Content-Type':'application/json'},body:body?JSON.stringify(body):undefined,throw:false});
    const result=timed?await this.withTimeout(request):await request;
    const json=readJson(result);
    if(result.status>=400)throw Object.assign(withDetails(json?.message||'Coppermind could not complete the request. Check connectivity and permissions.',result,json),{status:result.status});
    if(json)return json;
    if(!String(result.text??'').trim())return {};
    throw withDetails(NOT_COPPERMIND,result,json);
  }
};
