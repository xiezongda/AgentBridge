import { useEffect, useRef, useState } from 'react'
import {
  ArrowRight, ArrowUp, ChevronRight, CircleAlert, GitBranch, LibraryBig, LoaderCircle,
  MessageSquare, Network, Pencil, Plus, RotateCcw, Sparkles, Terminal, Trash2,
} from 'lucide-react'
import { api } from './api.js'
import FlowEditor, { editorDraft } from './FlowEditor.jsx'
import ModelLibrary from './ModelLibrary.jsx'
import './styles.css'

const statuses = { completed: '完成', successful: '完成', success: '完成', succeeded: '完成', ok: '完成', failed: '失败', error: '失败', partial: '部分完成', skipped: '已跳过', timed_out: '已超时', cancelled: '已取消' }
const templateDescriptions = {
  direct_sol: '用 Sol 单模型对话。',
  draft_review: 'Neon 起草，Astra 审阅。',
  parallel_synth: 'Neon 与 Sol 并行，Astra 综合。',
  blank: '从输入与输出节点开始搭建。',
}

function Rail({ activeId, rooms, page, health, onSelect, onNew, onLibrary }) {
  return (
    <aside className="rail">
      <div className="brand"><div className="brand-mark"><Network size={23} strokeWidth={1.8} /></div><div><strong>AgentBridge</strong><small>WORKBENCH</small></div></div>
      <button className="new-chat" onClick={onNew}><Plus size={17} />新建对话</button>
      <nav className="room-list" aria-label="对话列表">
        <h2>对话</h2>
        {rooms.map((room) => <button className={`room-button ${room.id === activeId && page === 'chat' ? 'active' : ''}`} key={room.id} onClick={() => onSelect(room.id)}><MessageSquare size={15} /><span>{room.title}</span></button>)}
        {!rooms.length && <small className="rail-empty">暂无对话</small>}
      </nav>
      <div className="rail-bottom">
        <button className={`room-button ${page === 'library' ? 'active' : ''}`} onClick={onLibrary}><LibraryBig size={16} /><span>模型库</span></button>
        <div className="connection"><span className={`indicator ${health}`} />{health === 'online' ? '本地服务已连接' : health === 'offline' ? '本地服务未连接' : '正在连接本地服务'}</div>
      </div>
    </aside>
  )
}

function FlowChips({ workflow, models }) {
  const cards = new Map(models.map((card) => [card.id, card]))
  return <div className="flow-chips" aria-label="当前流程">{(workflow?.nodes ?? []).map((node) => <span className={`flow-chip ${node.type}`} key={node.id}>{node.type === 'model' && <Sparkles size={12} />}{cards.get(node.config?.model_card_id)?.name ?? node.label ?? node.id}</span>)}</div>
}

const traceWorkspaceLabels = {
  workdir: '工作目录', extra_dirs: '额外目录', agy_project: 'AGY 项目', agy_mode: 'AGY 模式',
  enable_plugins_and_skills: 'AGY 插件与技能', agy_agent: 'AGY 智能体', sandbox_mode: 'Codex 沙箱模式',
  codex_profile: 'Codex 配置档', codex_trust: 'Codex 信任工作区', extra_cli_config: 'Codex 额外配置',
}

function TraceWorkspace({ settings }) {
  if (!settings) return null
  return (
    <details className="trace-attempts">
      <summary>工作区与插件</summary>
      <dl className="trace-workspace">
        {Object.entries(traceWorkspaceLabels).map(([field, label]) => {
          const value = settings[field]
          if (value == null) return null
          const display = typeof value === 'boolean' ? (value ? '启用' : '禁用')
            : Array.isArray(value) ? value.join('\n') || '无' : value
          return <div key={field}><dt>{label}</dt><dd>{display}</dd></div>
        })}
      </dl>
    </details>
  )
}

function TurnTrace({ turn, restoring, restore }) {
  const runs = Array.isArray(turn.node_runs) ? turn.node_runs : Object.values(turn.node_runs ?? {})
  return (
    <details className="turn-trace">
      <summary><ChevronRight size={14} className="trace-chevron" /><GitBranch size={15} />执行记录<span className={`trace-state ${turn.status}`}>{statuses[turn.status] ?? turn.status}</span><span className="trace-summary">{turn.total_model_calls ?? 0} 次模型调用{turn.workflow_version != null ? ` · 流程 v${turn.workflow_version}` : ''}</span></summary>
      <div className="trace-content">
        {runs.map((run, index) => {
          const attempts = run.attempt_log ?? (Array.isArray(run.attempts) ? run.attempts : [])
          return (
            <div className="trace-run" key={run.node_id ?? index}>
              <div className="trace-run-title"><span>{run.label || run.node_id || run.node_type}</span><span className={`trace-state ${run.status}`}>{statuses[run.status] ?? run.status}</span>{run.effective_effort && <span className="muted">{run.effective_effort}</span>}{run.latency_ms != null && <small>{Math.round(run.latency_ms)} ms</small>}</div>
              {run.output_text && <pre>{run.output_text}</pre>}
              {(run.error_message || run.error) && <p className="error-text">{run.error_message || run.error}</p>}
              <TraceWorkspace settings={run.effective_workspace} />
              {run.rendered_prompt && <details className="trace-attempts"><summary>本次提示词</summary><pre>{run.rendered_prompt}</pre></details>}
              {attempts.length > 1 && <details className="trace-attempts"><summary>{attempts.length} 次尝试</summary>{attempts.map((attempt, number) => <p key={number}>第 {number + 1} 次 · {statuses[attempt.status] ?? attempt.status}{attempt.latency_ms != null ? ` · ${Math.round(attempt.latency_ms)} ms` : ''}{attempt.error_message || attempt.error ? ` · ${attempt.error_message || attempt.error}` : ''}</p>)}</details>}
            </div>
          )
        })}
        <button className="secondary-button restore-button" disabled={restoring} onClick={() => restore(turn.id)}><RotateCcw size={14} />{restoring ? '恢复中' : '恢复此轮流程'}</button>
      </div>
    </details>
  )
}

function Chat({ room, models, pending, restoring, onEdit, onLibrary, onRename, onDelete, restore, onSend }) {
  const [message, setMessage] = useState('')
  const bottom = useRef(null)
  const turns = room.turns ?? []

  useEffect(() => { bottom.current?.scrollIntoView({ block: 'end' }) }, [turns.length, pending])

  async function submit(event) {
    event.preventDefault()
    if (!message.trim() || pending) return
    const sent = message
    setMessage('')
    if (!await onSend(sent)) setMessage((draft) => draft || sent)
  }

  return (
    <section className="chat-page">
      <header className="page-header chat-header">
        <div className="page-title"><h1>{room.title}</h1><small>流程 v{room.workflow_version}</small></div>
        <button className="icon-button" title="重命名对话" onClick={onRename}><Pencil size={16} /></button>
        <button className="icon-button danger" title="删除对话" onClick={onDelete}><Trash2 size={16} /></button>
        <button className="secondary-button" onClick={onEdit}><GitBranch size={16} />编辑流程</button>
        <button className="secondary-button" onClick={onLibrary}><LibraryBig size={16} />模型库</button>
      </header>
      <div className="chat-flow"><span><GitBranch size={14} />当前流程</span><FlowChips workflow={room.workflow} models={models} /></div>
      <div className="timeline" aria-live="polite">
        <div className="timeline-inner">
          {!turns.length && !pending && <div className="empty-chat"><div className="feature-icon"><MessageSquare size={27} /></div><h2>开始对话</h2><p>发送一条消息，运行当前流程。</p></div>}
          {turns.map((turn) => (
            <article key={turn.id} className="turn">
              <div className="user-message"><span>你</span><div>{turn.user_message}</div></div>
              <div className="assistant-message"><span className="assistant-icon"><Sparkles size={17} /></span><div>
                <h3>工作流响应</h3>
                {turn.final_response && <div className="assistant-text">{turn.final_response}</div>}
                {turn.error_message && <p className="error-text">{turn.error_message}</p>}
                {!turn.final_response && !turn.error_message && <p className="muted">当前流程未生成文本。</p>}
                <TurnTrace turn={turn} restore={restore} restoring={restoring} />
              </div></div>
            </article>
          ))}
          {pending && <article className="turn"><div className="user-message"><span>你</span><div>{pending}</div></div><div className="run-pending"><LoaderCircle size={17} className="spinning" />流程运行中</div></article>}
          <div ref={bottom} />
        </div>
      </div>
      <div className="composer-shell">
        <form className="composer" onSubmit={submit}>
          <textarea aria-label="消息" rows="2" placeholder="输入消息…" value={message} onChange={(event) => setMessage(event.target.value)} onKeyDown={(event) => {
            if (event.key === 'Enter' && !event.shiftKey && !event.nativeEvent.isComposing) { event.preventDefault(); event.currentTarget.form.requestSubmit() }
          }} />
          <button className="primary-button send-button" title="发送消息" aria-label="发送消息" disabled={!message.trim() || Boolean(pending)}><ArrowUp size={19} /></button>
        </form>
        <div className="composer-hint">Enter 发送 <span>·</span> Shift + Enter 换行</div>
      </div>
    </section>
  )
}

function Welcome({ templates, onNew, onLibrary }) {
  const featured = templates.filter((template) => template.id !== 'blank').slice(0, 3)
  return (
    <section className="welcome-page">
      <header className="page-header"><div className="page-title"><h1>工作台</h1></div><button className="secondary-button" onClick={onLibrary}><LibraryBig size={16} />模型库</button></header>
      <div className="welcome-content">
        <div className="feature-icon"><Network size={31} strokeWidth={1.6} /></div>
        <h2>多模型工作台</h2>
        <p>选择一个流程，开始对话。</p>
        {featured.length > 0 && <div className="starter-grid">{featured.map((template) => (
          <button key={template.id} className="starter-card" onClick={() => onNew(template.id)}>
            <div><GitBranch size={16} /><ArrowRight size={15} /></div>
            <strong>{template.name}</strong>
            <small>{templateDescriptions[template.id] ?? template.description}</small>
          </button>
        ))}</div>}
        <button className="primary-button" onClick={() => onNew()}><Plus size={17} />新建对话</button>
      </div>
    </section>
  )
}

function RoomDialog({ mode, templates, initial, onClose, onSave }) {
  const [title, setTitle] = useState(initial.title ?? '')
  const [template, setTemplate] = useState(initial.template ?? templates.find((item) => item.id === 'direct_sol')?.id ?? templates[0]?.id ?? '')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const creating = mode === 'new'
  async function submit(event) {
    event.preventDefault()
    setBusy(true)
    setError('')
    try { await onSave({ title: title.trim() || '新对话', ...(creating && template ? { template_id: template } : {}) }); onClose() }
    catch (err) { setError(err.message) }
    finally { setBusy(false) }
  }
  return (
    <div className="dialog-backdrop" onClick={busy ? undefined : onClose}>
      <form className="dialog" role="dialog" aria-modal="true" aria-label={creating ? '新建对话' : '重命名对话'} onSubmit={submit} onClick={(event) => event.stopPropagation()}>
        <h2>{creating ? '新建对话' : '重命名对话'}</h2>
        <label>对话名称<input autoFocus required={!creating} value={title} onChange={(event) => setTitle(event.target.value)} placeholder="新对话" /></label>
        {creating && templates.length > 0 && <label>流程模板<select value={template} onChange={(event) => setTemplate(event.target.value)}>{templates.map((item) => <option key={item.id} value={item.id}>{item.name}</option>)}</select></label>}
        {error && <p className="error-text" role="alert">{error}</p>}
        <div className="dialog-actions"><button className="secondary-button" type="button" disabled={busy} onClick={onClose}>取消</button><button className="primary-button" disabled={busy || (!creating && !title.trim())}>{busy ? '保存中' : creating ? '创建对话' : '保存名称'}</button></div>
      </form>
    </div>
  )
}

export default function App() {
  const [health, setHealth] = useState('loading')
  const [models, setModels] = useState([])
  const [rooms, setRooms] = useState([])
  const [templates, setTemplates] = useState([])
  const [activeId, setActiveId] = useState(null)
  const [details, setDetails] = useState({})
  const [page, setPage] = useState('chat')
  const [libraryReturn, setLibraryReturn] = useState('chat')
  const [workflow, setWorkflow] = useState(null)
  const [workflowVersion, setWorkflowVersion] = useState(null)
  const [dialog, setDialog] = useState(null)
  const [pending, setPending] = useState({})
  const [restoring, setRestoring] = useState(false)
  const [saving, setSaving] = useState(false)
  const [notice, setNotice] = useState(null)
  const room = details[activeId]

  function remember(record) {
    setDetails((current) => ({ ...current, [record.id]: record }))
    setRooms((current) => current.some((item) => item.id === record.id) ? current.map((item) => item.id === record.id ? record : item) : [record, ...current])
  }

  useEffect(() => {
    let disposed = false
    api.health().then(() => { if (!disposed) setHealth('online') }).catch(() => { if (!disposed) setHealth('offline') })
    Promise.all([api.models(), api.templates(), api.conversations()]).then(([cards, presets, conversations]) => {
      if (disposed) return
      setModels(cards)
      setTemplates(presets)
      setRooms(conversations)
      setActiveId((current) => current ?? conversations[0]?.id ?? null)
    }).catch((error) => { if (!disposed) setNotice({ text: error.message, error: true }) })
    return () => { disposed = true }
  }, [])

  useEffect(() => {
    if (!activeId) return
    const controller = new AbortController()
    api.conversation(activeId, controller.signal).then(remember).catch((error) => {
      if (!controller.signal.aborted) setNotice({ text: error.message, error: true })
    })
    return () => controller.abort()
  }, [activeId])

  async function refreshModels() {
    const records = await api.models()
    setModels(records)
    return records
  }

  async function refreshTemplates() {
    setTemplates(await api.templates())
  }

  function selectRoom(id) {
    setPage('chat')
    setActiveId(id)
    setNotice(null)
  }

  function openLibrary() {
    setLibraryReturn(page === 'editor' ? 'editor' : 'chat')
    setPage('library')
    setNotice(null)
  }

  function openEditor() {
    setWorkflow(editorDraft(room.workflow))
    setWorkflowVersion(room.workflow_version)
    setPage('editor')
    setNotice(null)
  }

  async function saveWorkflow() {
    setSaving(true)
    setNotice(null)
    try {
      const updated = await api.updateWorkflow(room.id, { workflow, workflow_version: workflowVersion })
      remember(updated)
      setWorkflowVersion(updated.workflow_version)
      setNotice({ text: `流程 v${updated.workflow_version} 已保存` })
    } catch (error) { setNotice({ text: error.message, error: true }) }
    finally { setSaving(false) }
  }

  async function sendMessage(message) {
    const id = room.id
    setPending((current) => ({ ...current, [id]: message }))
    setNotice(null)
    try {
      const turn = await api.sendMessage(id, message)
      setDetails((current) => {
        const record = current[id]
        if (!record) return current
        const turns = record.turns ?? []
        if (turns.some((item) => item.id === turn.id)) return current
        return { ...current, [id]: { ...record, turns: [...turns, turn] } }
      })
      return true
    } catch (error) { setNotice({ text: error.message, error: true }); return false }
    finally {
      setPending((current) => {
        const next = { ...current }
        delete next[id]
        return next
      })
    }
  }

  async function restoreWorkflow(turnId) {
    setRestoring(true)
    setNotice(null)
    try {
      const restored = await api.restoreWorkflow(room.id, turnId)
      remember(restored)
      setNotice({ text: `已恢复为流程 v${restored.workflow_version}` })
    } catch (error) { setNotice({ text: error.message, error: true }) }
    finally { setRestoring(false) }
  }

  async function deleteRoom() {
    if (!window.confirm(`删除对话“${room.title}”及其执行记录？`)) return
    try {
      await api.deleteConversation(room.id)
      const next = rooms.filter((item) => item.id !== room.id)
      setRooms(next)
      setDetails((current) => {
        const remaining = { ...current }
        delete remaining[room.id]
        return remaining
      })
      setActiveId((current) => current === room.id ? next[0]?.id ?? null : current)
    } catch (error) { setNotice({ text: error.message, error: true }) }
  }

  const openNew = (template) => setDialog({ mode: 'new', template })
  return (
    <div className="app-shell">
      <Rail rooms={rooms} activeId={activeId} page={page} health={health} onSelect={selectRoom} onNew={() => openNew()} onLibrary={openLibrary} />
      <main className="app-main">
        {notice && <div className={`toast ${notice.error ? 'error' : ''}`} role={notice.error ? 'alert' : 'status'}>{notice.error ? <CircleAlert size={16} /> : <Terminal size={16} />}<span>{notice.text}</span><button className="icon-button" aria-label="关闭通知" onClick={() => setNotice(null)}>×</button></div>}
        {page === 'library' ? <ModelLibrary models={models} refresh={refreshModels} onBack={() => setPage(libraryReturn)} />
          : page === 'editor' && room && workflow ? <FlowEditor conversation={room} workflow={workflow} setWorkflow={setWorkflow} models={models} templates={templates}
            refreshTemplates={refreshTemplates} onSave={saveWorkflow} saving={saving} onBack={() => setPage('chat')} onLibrary={openLibrary} />
          : room ? <Chat key={room.id} room={room} models={models} pending={pending[room.id]} restoring={restoring} restore={restoreWorkflow}
            onEdit={openEditor} onLibrary={openLibrary} onSend={sendMessage} onRename={() => setDialog({ mode: 'rename', title: room.title, id: room.id })} onDelete={deleteRoom} />
          : activeId ? <div className="loading-room"><LoaderCircle size={20} className="spinning" />正在加载对话</div>
          : <Welcome templates={templates} onNew={openNew} onLibrary={openLibrary} />}
      </main>
      {dialog && <RoomDialog mode={dialog.mode} initial={dialog} templates={templates} onClose={() => setDialog(null)} onSave={async (data) => {
        const record = dialog.mode === 'new' ? await api.createConversation(data) : await api.updateConversation(dialog.id, data)
        remember(record)
        selectRoom(record.id)
      }} />}
    </div>
  )
}
