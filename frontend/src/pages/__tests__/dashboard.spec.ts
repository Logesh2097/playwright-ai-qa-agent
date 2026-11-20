import { flushPromises, mount, RouterLinkStub } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { createMemoryHistory, createRouter } from 'vue-router'

import { apiKeysApi, executionsApi, githubApi, organizationApi } from '@/api/admin'
import { authApi } from '@/api/auth'
import { api } from '@/api/client'
import ExecutionProgress from '@/components/domain/ExecutionProgress.vue'
import ResultSummary from '@/components/domain/ResultSummary.vue'
import CodeViewer from '@/components/ui/CodeViewer.vue'
import DataTable from '@/components/ui/DataTable.vue'
import PaginationBar from '@/components/ui/PaginationBar.vue'
import StatCard from '@/components/ui/StatCard.vue'
import { parseSseChunk } from '@/composables/useExecutionStream'
import ExecutionLivePage from '@/pages/ExecutionLivePage.vue'
import ForgotPasswordPage from '@/pages/ForgotPasswordPage.vue'
import IntegrationsPage from '@/pages/IntegrationsPage.vue'
import ResetPasswordPage from '@/pages/ResetPasswordPage.vue'
import SettingsPage from '@/pages/SettingsPage.vue'
import { useAuthStore } from '@/stores/auth'
import type { LiveState } from '@/types/admin'
import type { OrgRole } from '@/types/api'

function signIn(role: OrgRole): void {
  const auth = useAuthStore()
  auth.applySession({
    access_token: 't',
    token_type: 'bearer',
    expires_in: 900,
    user: {
      id: 'u1',
      email: 'ada@acme.io',
      full_name: 'Ada',
      is_active: true,
      created_at: '2026-01-01T00:00:00Z',
      last_login_at: null,
    },
    memberships: [
      { organization_id: 'org1', organization_name: 'Acme', organization_slug: 'acme', role },
    ],
  } as never)
}

function router(path = '/') {
  const instance = createRouter({
    history: createMemoryHistory(),
    routes: [{ path: '/:all(.*)*', name: 'any', component: { template: '<div />' } }],
  })
  void instance.push(path)
  return instance
}

const state: LiveState = {
  id: '11111111-2222-3333-4444-555555555555',
  status: 'running',
  verdict: null,
  finished: false,
  totals: { total: 3, passed: 1, failed: 0, running: 1, pending: 1 },
  started_at: '2026-11-19T10:00:00Z',
  finished_at: null,
  results: [
    {
      id: 'r1',
      case_key: 'TC-0001',
      title: 'Sign in',
      status: 'passed',
      attempts: 1,
      failure_summary: null,
    },
    {
      id: 'r2',
      case_key: 'TC-0002',
      title: 'Points',
      status: 'running',
      attempts: 1,
      failure_summary: null,
    },
    {
      id: 'r3',
      case_key: 'TC-0003',
      title: 'Profile',
      status: 'pending',
      attempts: 0,
      failure_summary: null,
    },
  ],
  current: {
    result_id: 'r2',
    attempt: 1,
    step: {
      position: 3,
      phase: 'main',
      action: 'click',
      description: 'Calculate the points',
      status: 'passed',
      duration_ms: 57,
      page_url: null,
      error_type: null,
      error_message: null,
    },
  },
}

beforeEach(() => {
  setActivePinia(createPinia())
  vi.restoreAllMocks()
})

describe('shared components', () => {
  it('StatCard shows a missing figure as a dash, not as zero', () => {
    expect(mount(StatCard, { props: { label: 'Pass rate', value: null } }).text()).toContain('—')
    expect(mount(StatCard, { props: { label: 'Failed', value: 0 } }).text()).toContain('0')
  })

  it('DataTable renders columns, custom cells and an empty state', () => {
    const columns = [
      { key: 'name', label: 'Name' },
      { key: 'count', label: 'Count', numeric: true },
    ]
    const table = mount(DataTable, {
      props: {
        columns,
        rows: [{ id: 'a', name: '<b>x</b>', count: 2 }],
        rowKey: (row: object) => (row as { id: string }).id,
        caption: 'Things',
      },
      slots: { count: '<template #count="{ row }">#{{ row.count }}</template>' },
    })
    expect(table.find('caption').text()).toBe('Things')
    expect(table.findAll('th').map((th) => th.text())).toEqual(['Name', 'Count'])
    // Cell content is text: markup in data is shown, not rendered.
    expect(table.find('td').text()).toBe('<b>x</b>')
    expect(table.find('td b').exists()).toBe(false)
    expect(table.findAll('td')[1]!.text()).toBe('#2')
    const empty = mount(DataTable, {
      props: { columns, rows: [], rowKey: () => 'x', caption: 'Things', empty: 'None yet.' },
    })
    expect(empty.find('table').exists()).toBe(false)
    expect(empty.text()).toBe('None yet.')
  })

  it('PaginationBar reports the range and disables what cannot be done', async () => {
    const bar = mount(PaginationBar, { props: { offset: 25, limit: 25, count: 10, total: 35 } })
    expect(bar.text()).toContain('26–35 of 35')
    const [previous, next] = bar.findAll('button')
    expect(next!.attributes('disabled')).toBeDefined()
    await previous!.trigger('click')
    expect(bar.emitted('change')).toEqual([[0]])
    expect(
      mount(PaginationBar, { props: { offset: 0, limit: 25, count: 0, total: 0 } }).text(),
    ).toContain('0–0 of 0')
  })

  it('CodeViewer shows text as text and collapses long content', async () => {
    const content = Array.from({ length: 50 }, (_, n) => `line ${n} <script>`).join('\n')
    const viewer = mount(CodeViewer, { props: { content, label: 'Log', maxLines: 10 } })
    expect(viewer.find('script').exists()).toBe(false)
    expect(viewer.find('code').text()).toContain('line 9 <script>')
    expect(viewer.find('code').text()).not.toContain('line 10 ')
    const more = viewer.findAll('button').find((b) => b.text().includes('Show 40 more lines'))!
    await more.trigger('click')
    expect(viewer.find('code').text()).toContain('line 49')
  })

  it('ResultSummary counts every status separately and draws only passed as success', () => {
    const summary = mount(ResultSummary, {
      props: {
        totals: {
          total: 6,
          passed: 2,
          failed: 1,
          flaky: 1,
          needs_review: 1,
          skipped: 1,
          pass_rate: 0.4,
        },
        passRate: 0.4,
      },
    })
    expect(summary.findAll('.summary__segment--success.summary__segment')).toHaveLength(1)
    expect(summary.findAll('li').map((li) => li.text().replace(/\s+/g, ' '))).toEqual([
      '6 total',
      '2 passed',
      '1 failed',
      '1 flaky',
      '1 needs review',
      '1 skipped',
      'Pass rate 40.0%',
    ])
    expect(summary.find('[role="img"]').attributes('aria-label')).toBe(
      '2 passed, 1 failed, 1 flaky, 1 needs review, 1 skipped',
    )
  })

  it('ExecutionProgress shows finished cases over selected cases and the step the engine reported', () => {
    const progress = mount(ExecutionProgress, { props: { state } })
    expect(progress.text()).toContain('1 of 3 test cases finished')
    expect(progress.find('progress').attributes()).toMatchObject({ value: '1', max: '3' })
    expect(progress.text()).toContain('TC-0002')
    expect(progress.text()).toContain('3. Calculate the points')
    // No estimate of remaining time or percentage is invented.
    expect(progress.text()).not.toMatch(/remaining|%|eta/i)
    const waiting = mount(ExecutionProgress, {
      props: { state: { ...state, status: 'queued', current: null } },
    })
    expect(waiting.text()).toContain('Waiting for a worker to start.')
    expect(waiting.text()).toContain('No step reported yet.')
  })
})

describe('server-sent events', () => {
  it('splits complete records, keeps the unfinished rest and skips comments', () => {
    const first = parseSseChunk(
      'retry: 3000\n\nevent: step\nid: 4\ndata: {"seq":4}\n\n: keep-alive\n\nevent: sta',
    )
    expect(first.records).toEqual(
      [{ retry: '3000' }, { event: 'step', id: '4', data: '{"seq":4}' }].filter((r) => r.event),
    )
    expect(first.rest).toBe('event: sta')
    const second = parseSseChunk(first.rest + 'te\ndata: {"a":\ndata: 1}\n\n')
    expect(second.records).toEqual([{ event: 'state', data: '{"a":\n1}' }])
    expect(second.rest).toBe('')
  })
})

describe('ExecutionLivePage', () => {
  function stream(...chunks: string[]): Response {
    const encoder = new TextEncoder()
    const body = new ReadableStream<Uint8Array>({
      start(controller) {
        for (const chunk of chunks) controller.enqueue(encoder.encode(chunk))
        controller.close()
      },
    })
    return new Response(body, { status: 200 })
  }
  const sse = (event: string, data: unknown) => `event: ${event}\ndata: ${JSON.stringify(data)}\n\n`
  const step = {
    seq: 1,
    at: '2026-11-19T10:00:01Z',
    type: 'step_finished',
    result_id: 'r2',
    attempt: 1,
    position: 3,
    action: 'click',
    description: 'Calculate the points',
    status: 'passed',
    duration_ms: 57,
  }

  async function open(role: OrgRole, response: Response) {
    signIn(role)
    vi.spyOn(api, 'open').mockResolvedValue(response)
    const wrapper = mount(ExecutionLivePage, {
      props: { projectId: 'p1', executionId: state.id },
      global: { stubs: { RouterLink: RouterLinkStub } },
    })
    await flushPromises()
    await flushPromises()
    return wrapper
  }

  it('shows the state and the reported steps, and offers the report once finished', async () => {
    const finished = {
      ...state,
      status: 'completed',
      verdict: 'failed',
      finished: true,
      current: null,
    }
    const wrapper = await open(
      'developer',
      stream(sse('step', step), sse('state', state), sse('state', finished), sse('end', {})),
    )
    expect(wrapper.text()).toContain('TC-0002')
    expect(wrapper.find('.events').text()).toContain('3. Calculate the points')
    expect(wrapper.text()).toContain('Open the report')
    expect(wrapper.text()).toContain('Finished')
    expect(wrapper.text()).not.toContain('Cancel execution')
  })

  it('lets a contributor cancel after confirming, and not a viewer', async () => {
    const cancel = vi.spyOn(executionsApi, 'cancel').mockResolvedValue({} as never)
    // A stream that stays open: the execution is still running.
    const pending = new Response(
      new ReadableStream<Uint8Array>({
        start: (c) => c.enqueue(new TextEncoder().encode(sse('state', state))),
      }),
    )
    const wrapper = await open('developer', pending)
    const button = wrapper.findAll('button').find((b) => b.text() === 'Cancel execution')!
    await button.trigger('click')
    expect(cancel).not.toHaveBeenCalled()
    await wrapper
      .findAll('button')
      .find((b) => b.text() === 'Yes, cancel')!
      .trigger('click')
    await flushPromises()
    expect(cancel).toHaveBeenCalledWith('org1', 'p1', state.id)
    wrapper.unmount()

    setActivePinia(createPinia())
    const viewer = await open(
      'viewer',
      new Response(
        new ReadableStream<Uint8Array>({
          start: (c) => c.enqueue(new TextEncoder().encode(sse('state', state))),
        }),
      ),
    )
    expect(viewer.text()).not.toContain('Cancel execution')
    viewer.unmount()
  })

  it('says so when the connection is lost and keeps the last state on screen', async () => {
    vi.useFakeTimers()
    signIn('developer')
    let sent = false
    const open = vi
      .spyOn(api, 'open')
      .mockResolvedValueOnce(
        new Response(
          new ReadableStream<Uint8Array>({
            // The state arrives; the connection drops when more is asked for.
            pull(controller) {
              if (sent) controller.error(new TypeError('network down'))
              else controller.enqueue(new TextEncoder().encode(sse('state', state)))
              sent = true
            },
          }),
        ),
      )
      .mockRejectedValue(new Error('Could not reach the server.'))
    const wrapper = mount(ExecutionLivePage, {
      props: { projectId: 'p1', executionId: state.id },
      global: { stubs: { RouterLink: RouterLinkStub } },
    })
    await vi.advanceTimersByTimeAsync(50)
    expect(wrapper.text()).toContain('Connection lost. Reconnecting…')
    expect(wrapper.text()).toContain('1 of 3 test cases finished')
    expect(wrapper.text()).toContain('keeps running on the server')
    await vi.advanceTimersByTimeAsync(1100)
    expect(open.mock.calls.length).toBeGreaterThanOrEqual(2)
    wrapper.unmount()
    vi.useRealTimers()
  })
})

describe('password reset', () => {
  it('answers the same way for any address', async () => {
    const forgot = vi.spyOn(authApi, 'forgotPassword').mockResolvedValue({})
    const wrapper = mount(ForgotPasswordPage, { global: { stubs: { RouterLink: RouterLinkStub } } })
    await wrapper.find('input').setValue('nobody@example.org')
    await wrapper.find('form').trigger('submit')
    await flushPromises()
    expect(forgot).toHaveBeenCalledWith('nobody@example.org')
    expect(wrapper.text()).toContain('If an account exists for nobody@example.org')
  })

  it('needs the token from the link and two matching passwords', async () => {
    const reset = vi.spyOn(authApi, 'resetPassword').mockResolvedValue(undefined)
    const missing = mount(ResetPasswordPage, {
      global: { plugins: [router('/reset-password')], stubs: { RouterLink: RouterLinkStub } },
    })
    await flushPromises()
    expect(missing.text()).toContain('This link is not complete')

    const instance = router('/reset-password?token=' + 'x'.repeat(40))
    await instance.isReady()
    const wrapper = mount(ResetPasswordPage, {
      global: { plugins: [instance], stubs: { RouterLink: RouterLinkStub } },
    })
    const [first, second] = wrapper.findAll('input')
    await first!.setValue('a-long-new-passphrase')
    await second!.setValue('something else')
    expect(wrapper.text()).toContain('The two passwords are not the same.')
    expect(wrapper.find('button[type="submit"]').attributes('disabled')).toBeDefined()
    await second!.setValue('a-long-new-passphrase')
    await wrapper.find('form').trigger('submit')
    await flushPromises()
    expect(reset).toHaveBeenCalledWith('x'.repeat(40), 'a-long-new-passphrase')
    expect(wrapper.text()).toContain('Password changed')
    // The passwords do not stay in the page.
    expect(wrapper.findAll('input')).toHaveLength(0)
  })
})

describe('settings and integrations follow the role', () => {
  const members = [
    {
      id: 'm1',
      user_id: 'u1',
      email: 'ada@acme.io',
      full_name: 'Ada',
      role: 'admin',
      status: 'active',
      created_at: '',
    },
    {
      id: 'm2',
      user_id: 'u2',
      email: 'bob@acme.io',
      full_name: 'Bob',
      role: 'developer',
      status: 'active',
      created_at: '',
    },
  ]

  function mockSettings(): void {
    vi.spyOn(organizationApi, 'get').mockResolvedValue({
      id: 'org1',
      name: 'Acme',
      slug: 'acme',
      role: 'admin',
      created_at: '',
    })
    vi.spyOn(organizationApi, 'members').mockResolvedValue(members as never)
    vi.spyOn(organizationApi, 'roles').mockResolvedValue([
      { role: 'admin', permissions: ['executions:run', 'integrations:manage'] },
      { role: 'viewer', permissions: [] },
    ])
    vi.spyOn(organizationApi, 'auditLogs').mockResolvedValue({ items: [], next_cursor: null })
  }

  it('an admin can invite, change roles and read the audit log; nobody edits their own role', async () => {
    signIn('admin')
    mockSettings()
    const instance = router('/settings?tab=organization')
    await instance.isReady()
    const wrapper = mount(SettingsPage, { global: { plugins: [instance] } })
    await flushPromises()
    expect(wrapper.findAll('[role="tab"]').map((t) => t.text())).toContain('Audit log')
    expect(wrapper.text()).toContain('Invite someone')
    // One role selector: for Bob. Ada cannot change her own role.
    expect(wrapper.findAll('select.role')).toHaveLength(1)
    expect(wrapper.find('select.role').attributes('aria-label')).toBe('Role of bob@acme.io')
    expect(organizationApi.auditLogs).toHaveBeenCalled()
  })

  it('a viewer sees members and the role table but no way to change anything', async () => {
    signIn('viewer')
    mockSettings()
    const instance = router('/settings?tab=organization')
    await instance.isReady()
    const wrapper = mount(SettingsPage, { global: { plugins: [instance] } })
    await flushPromises()
    expect(wrapper.findAll('[role="tab"]').map((t) => t.text())).not.toContain('Audit log')
    expect(wrapper.text()).not.toContain('Invite someone')
    expect(wrapper.findAll('select.role')).toHaveLength(0)
    expect(wrapper.text()).toContain('bob@acme.io')
    expect(organizationApi.auditLogs).not.toHaveBeenCalled()
  })

  it('a new API key is shown once and connection secrets are cleared after sending', async () => {
    signIn('admin')
    vi.spyOn(githubApi, 'connections').mockResolvedValue([])
    vi.spyOn(apiKeysApi, 'list').mockResolvedValue([])
    vi.spyOn(organizationApi, 'browser').mockResolvedValue({
      configured: true,
      connection: null,
      restricted: false,
      tools: [],
    })
    vi.spyOn(apiKeysApi, 'create').mockResolvedValue({
      id: 'k1',
      name: 'pipeline',
      key_prefix: 'qak_x',
      scopes: [],
      project_id: null,
      created_at: '',
      last_used_at: null,
      expires_at: null,
      revoked_at: null,
      key: 'qak_secret_value',
    })
    const instance = router('/integrations?tab=ci')
    await instance.isReady()
    const wrapper = mount(IntegrationsPage, { global: { plugins: [instance] } })
    await flushPromises()
    await wrapper
      .findAll('button')
      .find((b) => b.text() === 'Create API key')!
      .trigger('click')
    const form = wrapper.findAll('form').find((f) => f.text().includes('What the key may do'))!
    await form.find('input.input').setValue('pipeline')
    await form.trigger('submit')
    await flushPromises()
    expect(apiKeysApi.create).toHaveBeenCalledWith('org1', {
      name: 'pipeline',
      scopes: ['executions:write', 'executions:read'],
      project_id: null,
      expires_in_days: 90,
    })
    expect(wrapper.text()).toContain('Copy this key now. It is not shown again.')
    expect(wrapper.text()).toContain('qak_secret_value')
    await wrapper
      .findAll('button')
      .find((b) => b.text() === 'I have stored it')!
      .trigger('click')
    expect(wrapper.text()).not.toContain('qak_secret_value')
  })

  it('a non-admin is told who manages integrations and no admin data is requested', async () => {
    signIn('developer')
    const connections = vi.spyOn(githubApi, 'connections').mockResolvedValue([])
    const keys = vi.spyOn(apiKeysApi, 'list').mockResolvedValue([])
    vi.spyOn(organizationApi, 'browser').mockResolvedValue({
      configured: false,
      connection: null,
      restricted: false,
      tools: [],
    })
    const instance = router('/integrations')
    await instance.isReady()
    const wrapper = mount(IntegrationsPage, { global: { plugins: [instance] } })
    await flushPromises()
    expect(wrapper.text()).toContain('Only organization admins manage GitHub connections.')
    expect(connections).not.toHaveBeenCalled()
    expect(keys).not.toHaveBeenCalled()
  })
})
