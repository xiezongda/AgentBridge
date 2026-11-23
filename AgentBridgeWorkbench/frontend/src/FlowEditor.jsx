import { useEffect, useMemo, useState } from 'react'
import {
  Background, BackgroundVariant, Controls, Handle, MiniMap, Position, ReactFlow,
  ReactFlowProvider, useReactFlow,
} from '@xyflow/react'
import {
  ArrowDown, ArrowLeft, ArrowUp, Check, CircleAlert, Download, FileText, FolderCog, GitBranch, LibraryBig,
  MessageSquare, Plus, Save, Sparkles, Terminal, Trash2,
  Upload,
} from 'lucide-react'
import '@xyflow/react/dist/style.css'
import { api } from './api.js'
import { ExportDialog, ImportDialog } from './WorkflowTransfer.jsx'

const cardMime = 'application/x-agentbridge-model'
const efforts = ['none', 'minimal', 'low', 'medium', 'high', 'xhigh', 'max', 'ultra']
const sandboxes = ['read-only', 'workspace-write', 'danger-full-access']
const agyModes = ['plan', 'accept-edits', 'auto']
const nodeMeta = {
  user_input: { label: '用户输入', icon: MessageSquare },
  context_input: { label: '上下文', icon: FileText },
  model: { label: '模型', icon: Sparkles },
  final_output: { label: '最终输出', icon: Terminal },
}
const scheduleFields = [
  ['max_concurrency', '并发', 1],
  ['max_calls_per_turn', '调用上限', 1],
  ['default_node_timeout_s', '节点超时 (s)', 1],
  ['turn_timeout_s', '轮次超时 (s)', 1],
  ['max_context_chars', '上下文 (字符)', 200],
  ['default_max_retries', '重试', 0],
]

function WorkbenchNode({ id, data, selected }) {
  const meta = nodeMeta[data.nodeType] ?? nodeMeta.model
  const Icon = meta.icon
  return (
    <div data-flow-node={id} className={`flow-node ${data.nodeType} ${selected ? 'selected' : ''}`}>
      {['model', 'final_output'].includes(data.nodeType) && <Handle type="target" position={Position.Left} />}
      <div className="flow-node-head"><Icon size={15} /><span>{meta.label}</span></div>
      <div className="flow-node-content">
        <strong>{data.title}</strong>
        {data.subtitle && <small>{data.subtitle}</small>}
      </div>
      {data.nodeType !== 'final_output' && <Handle type="source" position={Position.Right} />}
    </div>
  )
}

const flowTypes = { workbench: WorkbenchNode }
const makeId = (prefix) => `${prefix}_${globalThis.crypto?.randomUUID?.() ?? `${Date.now()}_${Math.random().toString(36).slice(2, 10)}`}`

export function editorDraft(workflow) {
  const draft = structuredClone(workflow)
  draft.edges.forEach((edge) => { if (!edge.id) edge.id = makeId('edge') })
  if (draft.workspace) {
    const { sandbox_mode_override, ...workspace } = draft.workspace
    if (sandbox_mode_override != null) workspace.sandbox_mode = sandbox_mode_override
    draft.workspace = workspace
  }
  for (const node of draft.nodes) {
    if (node.type !== 'model') continue
    const config = node.config ?? (node.config = {})
    const settings = { ...node, ...config }
    const sandbox = settings.sandbox_mode_override ?? settings.sandbox_mode
    delete node.sandbox_mode_override
    delete node.sandbox_mode
    delete config.sandbox_mode
    if (sandbox != null) config.sandbox_mode_override = sandbox
  }
  return draft
}

function ScheduleBar({ schedule, onChange }) {
  return (
    <section className="schedule-bar" aria-label="调度设置">
      {scheduleFields.map(([key, label, minimum]) => (
        <label key={key} title={key === 'default_node_timeout_s' ? '限制模型卡片超时。节点可指定自己的超时，本轮仍受轮次超时限制。' : undefined}>{label}
          <input type="number" min={minimum} step="1" value={schedule[key] ?? minimum}
            onChange={(event) => onChange(key, Number(event.target.value))} />
        </label>
      ))}
      <label>错误策略
        <select value={schedule.failure_policy ?? 'stop_on_error'} onChange={(event) => onChange('failure_policy', event.target.value)}>
          <option value="stop_on_error">遇错停止</option>
          <option value="continue_on_error">遇错继续</option>
        </select>
      </label>
    </section>
  )
}

function ModelPalette({ models, addModel, onLibrary }) {
  return (
    <aside className="palette">
      <div className="panel-heading"><h2>节点</h2><button className="icon-button" title="打开模型库" onClick={onLibrary}><LibraryBig size={16} /></button></div>
      <div className="palette-list">
        {['user_input', 'context_input', 'final_output'].map((type) => {
          const Icon = nodeMeta[type].icon
          return <button className="palette-action" key={type} onClick={() => addModel(type)}><Icon size={15} />{nodeMeta[type].label}<Plus size={13} /></button>
        })}
      </div>
      <div className="palette-section-heading">模型卡片</div>
      <div className="palette-list">
        {models.map((card) => (
          <button className="palette-card" key={card.id} draggable
            onDragStart={(event) => { event.dataTransfer.setData(cardMime, card.id); event.dataTransfer.effectAllowed = 'copy' }}
            onClick={() => addModel('model', card.id)}>
            <span className={`provider-icon ${card.provider}`}><Sparkles size={15} /></span>
            <span><strong>{card.name}</strong><small>{card.model_id}</small></span>
            <Plus size={13} className="palette-plus" />
          </button>
        ))}
      </div>
    </aside>
  )
}

function OptionalNumber({ label, minimum, value, onChange }) {
  return (
    <label>{label}<input type="number" min={minimum} step="1" placeholder="继承默认值" value={value ?? ''}
      onChange={(event) => onChange(event.target.value === '' ? undefined : Number(event.target.value))} /></label>
  )
}

function OptionalSelect({ label, field, options, value, onChange }) {
  return (
    <label>{label}<select name={field} value={value ?? ''} onChange={(event) => onChange(event.target.value || undefined)}>
      <option value="">继承</option>
      {options.map((option) => <option key={option}>{option}</option>)}
    </select></label>
  )
}

function OptionalFlag({ label, field, value, onChange }) {
  return (
    <label>{label}<select name={field} value={value == null ? '' : String(value)} onChange={(event) => {
      const next = event.target.value
      onChange(next === '' ? undefined : next === 'true')
    }}>
      <option value="">继承</option><option value="true">启用</option><option value="false">禁用</option>
    </select></label>
  )
}

function OptionalList({ label, field, value, placeholder, onChange }) {
  return (
    <div className="workspace-list-field">
      <label>{label}<select aria-label={`${label}设置来源`} value={value == null ? 'inherit' : 'override'}
        onChange={(event) => onChange(event.target.value === 'inherit' ? undefined : [])}>
        <option value="inherit">继承</option><option value="override">自定义</option>
      </select></label>
      {value != null && <textarea rows="2" name={field} aria-label={label} defaultValue={value.join('\n')} placeholder={placeholder}
        onChange={(event) => onChange(event.target.value.split('\n').map((line) => line.trim()).filter(Boolean))} />}
    </div>
  )
}

function WorkspaceFields({ settings, update, includeSandbox = false }) {
  return (
    <div className="workspace-fields">
      <label>工作目录<input name="workdir" className="tech-input" value={settings.workdir ?? ''} onChange={(event) => update('workdir', event.target.value || undefined)} placeholder="继承默认目录" /></label>
      <OptionalList label="额外目录" field="extra_dirs" value={settings.extra_dirs} placeholder="每行一个目录 · 留空表示无" onChange={(value) => update('extra_dirs', value)} />
      <label>AGY 项目<input name="agy_project" className="tech-input" value={settings.agy_project ?? ''} onChange={(event) => update('agy_project', event.target.value || undefined)} placeholder="继承默认项目" /></label>
      <OptionalSelect label="AGY 模式" field="agy_mode" options={agyModes} value={settings.agy_mode} onChange={(value) => update('agy_mode', value)} />
      <OptionalFlag label="AGY 插件与技能" field="enable_plugins_and_skills" value={settings.enable_plugins_and_skills} onChange={(value) => update('enable_plugins_and_skills', value)} />
      <label>AGY 智能体<input name="agy_agent" className="tech-input" value={settings.agy_agent ?? ''} onChange={(event) => update('agy_agent', event.target.value || undefined)} placeholder="继承默认智能体" /></label>
      {includeSandbox && <OptionalSelect label="Codex 沙箱模式" field="sandbox_mode" options={sandboxes} value={settings.sandbox_mode} onChange={(value) => update('sandbox_mode', value)} />}
      <label>Codex 配置档<input name="codex_profile" className="tech-input" value={settings.codex_profile ?? ''} onChange={(event) => update('codex_profile', event.target.value || undefined)} placeholder="继承默认配置档" /></label>
      <OptionalFlag label="Codex 信任工作区" field="codex_trust" value={settings.codex_trust} onChange={(value) => update('codex_trust', value)} />
      <OptionalList label="Codex 额外配置" field="extra_cli_config" value={settings.extra_cli_config} placeholder={'每行一条配置 · 如 plugins."pdf".enabled=true'} onChange={(value) => update('extra_cli_config', value)} />
    </div>
  )
}

function WorkspaceDialog({ workspace, close, apply }) {
  const [settings, setSettings] = useState({ ...workspace })

  function update(key, value) {
    setSettings((current) => {
      const next = { ...current, [key]: value }
      if (value === undefined) delete next[key]
      return next
    })
  }

  return (
    <div className="dialog-backdrop" onClick={close}>
      <form className="dialog workspace-dialog" role="dialog" aria-modal="true" aria-label="Workspace & Plugins"
        onClick={(event) => event.stopPropagation()} onSubmit={(event) => { event.preventDefault(); apply(settings); close() }}>
        <h2>工作区与插件</h2>
        <p className="muted">流程默认值优先于模型卡片，节点可单独覆盖。</p>
        <WorkspaceFields settings={settings} update={update} includeSandbox />
        <div className="dialog-actions">
          <button className="secondary-button" type="button" onClick={close}>取消</button>
          <button className="primary-button"><Check size={15} />应用默认值</button>
        </div>
      </form>
    </div>
  )
}

function NodeInspector({ node, workflow, models, update, remove, preview }) {
  if (!node) return <aside className="inspector"><div className="panel-heading"><h2>节点设置</h2></div><p className="panel-empty">选择画布上的节点以编辑设置。</p></aside>
  const config = node.config ?? {}
  const sources = workflow.edges.filter((edge) => edge.target === node.id).map((edge) => edge.source)
  const currentOrder = [...new Set([...(config.input_order ?? []).filter((id) => sources.includes(id)), ...sources])]
  const reorder = (index, delta) => {
    const next = [...currentOrder]
    const destination = index + delta
    const [id] = next.splice(index, 1)
    next.splice(destination, 0, id)
    update('input_order', next)
  }
  return (
    <aside className="inspector">
      <div className="panel-heading"><h2>{nodeMeta[node.type]?.label ?? node.type}</h2><button className="icon-button danger" title="删除节点" onClick={remove}><Trash2 size={16} /></button></div>
      {node.type === 'context_input' && <div className="inspector-fields"><label>上下文内容<textarea rows="9" value={config.text ?? ''} onChange={(event) => update('text', event.target.value)} /></label></div>}
      {node.type === 'user_input' && <p className="panel-empty">读取本轮发送的用户消息。</p>}
      {node.type === 'final_output' && <p className="panel-empty">汇集上游结果并发送到对话。</p>}
      {node.type === 'model' && (
        <>
          <div className="inspector-fields">
            <label>模型卡片
              <select value={config.model_card_id ?? ''} onChange={(event) => update('model_card_id', event.target.value)}>
                <option value="" disabled>选择模型</option>
                {config.model_card_id && !models.some((card) => card.id === config.model_card_id) && <option value={config.model_card_id}>{config.model_card_id} · 已移除</option>}
                {models.map((card) => <option key={card.id} value={card.id}>{card.name}</option>)}
              </select>
            </label>
            <label>系统提示词<textarea rows="4" value={config.system_prompt ?? ''} onChange={(event) => update('system_prompt', event.target.value)} /></label>
            <div className="form-pair">
              <label className="check-label"><input type="checkbox" checked={Boolean(config.include_history)} onChange={(event) => update('include_history', event.target.checked)} />包含历史</label>
              <label>历史轮数<input type="number" min="0" step="1" disabled={!config.include_history} value={config.history_turns ?? 0} onChange={(event) => update('history_turns', Number(event.target.value))} /></label>
            </div>
            {currentOrder.length > 0 && <fieldset className="input-order"><legend>上游顺序</legend>{currentOrder.map((id, index) => (
              <div key={id}><span>{workflow.nodes.find((item) => item.id === id)?.label ?? id}</span>
                <button className="icon-button" disabled={index === 0} title="上移" onClick={() => reorder(index, -1)}><ArrowUp size={13} /></button>
                <button className="icon-button" disabled={index === currentOrder.length - 1} title="下移" onClick={() => reorder(index, 1)}><ArrowDown size={13} /></button>
              </div>
            ))}</fieldset>}
            <div className="form-pair">
              <label>推理强度<select value={config.effort_override ?? ''} onChange={(event) => update('effort_override', event.target.value || undefined)}><option value="">继承</option>{efforts.map((effort) => <option key={effort}>{effort}</option>)}</select></label>
              <OptionalSelect label="沙箱模式" field="sandbox_mode_override" options={sandboxes} value={config.sandbox_mode_override} onChange={(value) => update('sandbox_mode_override', value)} />
            </div>
            <div className="form-pair">
              <OptionalNumber label="超时 (s)" minimum={1} value={config.timeout_s_override} onChange={(value) => update('timeout_s_override', value)} />
              <OptionalNumber label="最大重试" minimum={0} value={config.max_retries_override} onChange={(value) => update('max_retries_override', value)} />
            </div>
          </div>
          <details className="node-workspace" key={`workspace:${node.id}`}>
            <summary><FolderCog size={15} />工作区与插件</summary>
            <WorkspaceFields settings={config} update={update} />
          </details>
          {preview}
        </>
      )}
    </aside>
  )
}

function PromptPreview({ conversationId, workflow, nodeId }) {
  const [sample, setSample] = useState('')
  const [state, setState] = useState(null)
  useEffect(() => {
    const controller = new AbortController()
    const timer = setTimeout(() => {
      api.previewPrompt({ conversation_id: conversationId, workflow, node_id: nodeId, sample_user_message: sample }, controller.signal)
        .then((result) => setState({ result }))
        .catch((error) => { if (!controller.signal.aborted) setState({ error: error.message }) })
    }, 250)
    return () => { clearTimeout(timer); controller.abort() }
  }, [conversationId, nodeId, sample, workflow])

  return (
    <section className="prompt-preview">
      <h3><Terminal size={15} />提示词预览</h3>
      <label>样例消息<textarea rows="2" value={sample} onChange={(event) => setSample(event.target.value)} placeholder="输入一条试用消息" /></label>
      {state?.error && <p className="error-text" role="alert">{state.error}</p>}
      {state?.result && <><small>{state.result.prompt.length} 字符{state.result.truncated ? ' · 已截断' : ''}</small><pre>{state.result.prompt || '此节点当前没有提示词内容。'}</pre></>}
    </section>
  )
}

function TemplatesDialog({ templates, workflow, close, apply, refresh }) {
  const [id, setId] = useState('')
  const [name, setName] = useState('')
  const [description, setDescription] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')

  function choose(nextId) {
    const current = templates.find((template) => template.id === nextId)
    setId(nextId)
    setName(current?.name ?? '')
    setDescription(current?.description ?? '')
    setError('')
  }

  async function save(event) {
    event.preventDefault()
    setBusy(true)
    setError('')
    try {
      const data = { name: name.trim(), description, workflow }
      const result = id ? await api.updateTemplate(id, data) : await api.createTemplate(data)
      await refresh()
      setId(result.id)
    } catch (err) { setError(err.message) }
    finally { setBusy(false) }
  }

  async function remove() {
    if (!window.confirm(`删除模板“${name}”？`)) return
    setBusy(true)
    setError('')
    try { await api.deleteTemplate(id); await refresh(); choose('') }
    catch (err) { setError(err.message) }
    finally { setBusy(false) }
  }

  return (
    <div className="dialog-backdrop" onClick={close}>
      <form className="dialog" onSubmit={save} onClick={(event) => event.stopPropagation()} role="dialog" aria-modal="true" aria-label="流程模板">
        <h2>流程模板</h2>
        <label>选择模板<select value={id} onChange={(event) => choose(event.target.value)}><option value="">新建模板</option>{templates.map((template) => <option value={template.id} key={template.id}>{template.name}</option>)}</select></label>
        <label>名称<input required value={name} onChange={(event) => setName(event.target.value)} /></label>
        <label>描述<textarea rows="3" value={description} onChange={(event) => setDescription(event.target.value)} /></label>
        {error && <p className="error-text" role="alert">{error}</p>}
        <div className="dialog-actions">
          {id && <><button type="button" title="删除模板" className="icon-button danger" disabled={busy} onClick={remove}><Trash2 size={16} /></button><button type="button" className="secondary-button" disabled={busy} onClick={() => { apply(templates.find((template) => template.id === id).workflow); close() }}>应用模板</button></>}
          <button className="secondary-button" type="button" onClick={close}>关闭</button>
          <button className="primary-button" disabled={busy || !name.trim()}><Save size={15} />保存模板</button>
        </div>
      </form>
    </div>
  )
}

function Editor({ conversation, workflow, setWorkflow, models, templates, refreshTemplates, onSave, saving, onBack, onLibrary }) {
  const { screenToFlowPosition } = useReactFlow()
  const [selectedNode, setSelectedNode] = useState(null)
  const [selectedEdges, setSelectedEdges] = useState([])
  const [dialog, setDialog] = useState(null)
  const [validation, setValidation] = useState(null)
  const selected = workflow.nodes.find((node) => node.id === selectedNode)
  const cards = useMemo(() => new Map(models.map((card) => [card.id, card])), [models])

  useEffect(() => {
    const controller = new AbortController()
    const timer = setTimeout(() => {
      api.validate(workflow, false, controller.signal).then(setValidation)
        .catch((error) => { if (!controller.signal.aborted) setValidation({ valid: false, errors: [error.message] }) })
    }, 250)
    return () => { clearTimeout(timer); controller.abort() }
  }, [workflow])

  const flowNodes = workflow.nodes.map((node) => {
    const card = cards.get(node.config?.model_card_id)
    return {
      id: node.id, type: 'workbench', position: node.position ?? { x: 0, y: 0 }, selected: selectedNode === node.id,
      data: {
        nodeType: node.type,
        title: card?.name ?? node.label ?? nodeMeta[node.type]?.label ?? node.type,
        subtitle: card?.model_id ?? (node.type === 'context_input' ? node.config?.text?.trim() : undefined),
      },
    }
  })
  const flowEdges = workflow.edges.map((edge) => ({ ...edge, selected: selectedEdges.includes(edge.id), type: 'smoothstep' }))

  function isValidConnection({ source, target }) {
    if (!source || !target || source === target || workflow.edges.some((edge) => edge.source === source && edge.target === target)) return false
    const reachable = new Set()
    const queue = [target]
    while (queue.length) {
      const id = queue.pop()
      if (id === source) return false
      if (reachable.has(id)) continue
      reachable.add(id)
      workflow.edges.forEach((edge) => { if (edge.source === id) queue.push(edge.target) })
    }
    return true
  }

  function onConnect(connection) {
    if (!isValidConnection(connection)) return
    const { source, target } = connection
    setWorkflow((current) => ({ ...current, edges: [...current.edges, { id: makeId('edge'), source, target }] }))
  }

  function addNode(type, modelId, position) {
    const id = makeId(type)
    const config = type === 'model' ? { model_card_id: modelId, system_prompt: '', include_history: true, history_turns: 4, input_order: [] } : {}
    if (type === 'context_input') config.text = ''
    setWorkflow((current) => ({
      ...current,
      nodes: [...current.nodes, {
        id, type, config, label: cards.get(modelId)?.name ?? nodeMeta[type].label,
        position: position ?? { x: 100 + current.nodes.length * 30, y: 120 + current.nodes.length * 27 },
      }],
    }))
    setSelectedNode(id)
  }

  function removeGraphParts(current, nodeIds, edgeIds) {
    const removed = current.edges.filter((edge) => edgeIds.has(edge.id) || nodeIds.has(edge.source) || nodeIds.has(edge.target))
    return {
      ...current,
      edges: current.edges.filter((edge) => !removed.includes(edge)),
      nodes: current.nodes.filter((node) => !nodeIds.has(node.id)).map((node) => {
        if (!node.config?.input_order?.length) return node
        const lostSources = new Set(removed.filter((edge) => edge.target === node.id).map((edge) => edge.source))
        if (!lostSources.size) return node
        return { ...node, config: { ...node.config, input_order: node.config.input_order.filter((id) => !lostSources.has(id)) } }
      }),
    }
  }

  function onNodesChange(changes) {
    const positions = new Map(changes.filter((change) => change.type === 'position' && change.position).map((change) => [change.id, change.position]))
    const removed = new Set(changes.filter((change) => change.type === 'remove').map((change) => change.id))
    if (removed.size) setWorkflow((current) => removeGraphParts(current, removed, new Set()))
    if (positions.size) setWorkflow((current) => ({ ...current, nodes: current.nodes.map((node) => positions.has(node.id) ? { ...node, position: positions.get(node.id) } : node) }))
  }

  function onEdgesChange(changes) {
    const removed = new Set(changes.filter((change) => change.type === 'remove').map((change) => change.id))
    if (removed.size) setWorkflow((current) => removeGraphParts(current, new Set(), removed))
    const selections = changes.filter((change) => change.type === 'select')
    if (selections.length) setSelectedEdges((current) => {
      const next = new Set(current)
      selections.forEach(({ id, selected: isSelected }) => { if (isSelected) next.add(id); else next.delete(id) })
      return [...next]
    })
  }

  function onDrop(event) {
    event.preventDefault()
    const card = cards.get(event.dataTransfer.getData(cardMime))
    if (!card) return
    const targetId = event.target.closest('[data-flow-node]')?.dataset.flowNode
    const target = workflow.nodes.find((node) => node.id === targetId)
    if (target?.type === 'model') {
      setWorkflow((current) => ({ ...current, nodes: current.nodes.map((node) => node.id === targetId ? {
        ...node, label: card.name, config: { ...node.config, model_card_id: card.id },
      } : node) }))
      setSelectedNode(targetId)
    } else if (!target) {
      addNode('model', card.id, screenToFlowPosition({ x: event.clientX, y: event.clientY }))
    }
  }

  function updateConfig(key, value) {
    setWorkflow((current) => ({ ...current, nodes: current.nodes.map((node) => {
      if (node.id !== selectedNode) return node
      const config = { ...node.config, [key]: value }
      if (value === undefined) delete config[key]
      return { ...node, config, label: key === 'model_card_id' ? cards.get(value)?.name ?? node.label : node.label }
    }) }))
  }

  function applyWorkflow(next) {
    setWorkflow(editorDraft(next))
    setSelectedNode(null)
    setSelectedEdges([])
  }

  function applyWorkspace(settings) {
    setWorkflow((current) => {
      const next = { ...current }
      if (Object.keys(settings).length) next.workspace = settings
      else delete next.workspace
      return next
    })
  }

  const messages = [...(validation?.errors ?? []), ...(validation?.warnings ?? [])]
  return (
    <section className="editor-page">
      <header className="page-header editor-header">
        <button className="icon-button" title="返回对话" onClick={onBack}><ArrowLeft size={20} /></button>
        <div className="page-title"><h1>编辑流程</h1><small>{conversation.title} · v{conversation.workflow_version}</small></div>
        <div className="editor-tools" aria-label="流程操作">
          <button className="secondary-button" onClick={() => setDialog('workspace')} title="工作区与插件"><FolderCog size={16} /><span>工作区与插件</span></button>
          <button className="secondary-button" onClick={() => setDialog('export')} title="导出流程"><Download size={16} /><span>导出</span></button>
          <button className="secondary-button" onClick={() => setDialog('import')} title="导入流程"><Upload size={16} /><span>导入</span></button>
          <button className="secondary-button" onClick={() => setDialog('templates')} title="流程模板"><LibraryBig size={16} /><span>流程模板</span></button>
          <button className="primary-button" disabled={saving} onClick={onSave}><Save size={16} />{saving ? '保存中' : '保存流程'}</button>
        </div>
      </header>
      <ScheduleBar schedule={workflow.schedule ?? {}} onChange={(key, value) => setWorkflow((current) => ({ ...current, schedule: { ...current.schedule, [key]: value } }))} />
      <div className="editor-workspace">
        <ModelPalette models={models} addModel={addNode} onLibrary={onLibrary} />
        <div className="canvas">
          <ReactFlow nodes={flowNodes} edges={flowEdges} nodeTypes={flowTypes} onNodesChange={onNodesChange} onEdgesChange={onEdgesChange}
            onNodeClick={(_, node) => setSelectedNode(node.id)} onPaneClick={() => setSelectedNode(null)}
            onConnect={onConnect} isValidConnection={isValidConnection} onDrop={onDrop}
            onDragOver={(event) => { event.preventDefault(); event.dataTransfer.dropEffect = 'copy' }}
            colorMode="dark" fitView fitViewOptions={{ padding: 0.24, maxZoom: 1.25 }} deleteKeyCode={['Backspace', 'Delete']}>
            <Background variant={BackgroundVariant.Dots} gap={24} size={1.4} color="#373941" />
            <MiniMap nodeColor={(node) => ({ user_input: '#65aeca', context_input: '#a596d4', model: '#e8955d', final_output: '#7cb29b' })[node.data.nodeType] ?? '#777'} pannable zoomable />
            <Controls />
          </ReactFlow>
        </div>
        <NodeInspector node={selected} workflow={workflow} models={models} update={updateConfig}
          remove={() => { setWorkflow((current) => removeGraphParts(current, new Set([selectedNode]), new Set())); setSelectedNode(null) }}
          preview={selected?.type === 'model' && <PromptPreview key={`preview:${selected.id}`} conversationId={conversation.id} workflow={workflow} nodeId={selected.id} />} />
      </div>
      <footer className="canvas-status">
        <span><GitBranch size={14} />{workflow.nodes.length} 个节点 · {workflow.edges.length} 条连线</span>
        {validation && <span className={validation.valid ? 'pass-text' : 'error-text'} title={messages.join('\n')}>
          {validation.valid ? <Check size={14} /> : <CircleAlert size={14} />}{messages[0] ?? '流程结构有效'}
        </span>}
      </footer>
      {dialog === 'workspace' && <WorkspaceDialog workspace={workflow.workspace} close={() => setDialog(null)} apply={applyWorkspace} />}
      {dialog === 'export' && <ExportDialog workflow={workflow} title={conversation.title} close={() => setDialog(null)} />}
      {dialog === 'import' && <ImportDialog models={models} close={() => setDialog(null)} apply={applyWorkflow} refreshTemplates={refreshTemplates} />}
      {dialog === 'templates' && <TemplatesDialog templates={templates} workflow={workflow} close={() => setDialog(null)} refresh={refreshTemplates} apply={applyWorkflow} />}
    </section>
  )
}

export default function FlowEditor(props) {
  return <ReactFlowProvider><Editor {...props} /></ReactFlowProvider>
}
