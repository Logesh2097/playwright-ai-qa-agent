import { flushPromises, mount } from '@vue/test-utils'
import { describe, expect, it, vi } from 'vitest'

import { ApiError } from '@/api/client'
import CredentialForm from '@/components/domain/CredentialForm.vue'
import EnvironmentForm from '@/components/domain/EnvironmentForm.vue'
import { useFormErrors } from '@/composables/useFormErrors'
import type { Credential, CredentialInput, Environment, EnvironmentInput } from '@/types/api'

const stored: Credential = {
  id: 'c1',
  environment_id: 'e1',
  name: 'customer',
  role_label: 'customer',
  kind: 'username_password',
  source: 'stored',
  field_names: ['password', 'username'],
  external_ref: null,
  is_test_account: true,
  setup_instructions: 'Has 500 loyalty points.',
  login_recipe: [],
  last_rotated_at: null,
  created_at: '2026-01-01T00:00:00Z',
  updated_at: '2026-01-01T00:00:00Z',
}

describe('CredentialForm', () => {
  it('requires the test-account confirmation before a new credential can be stored', async () => {
    const save = vi.fn(async () => {})
    const wrapper = mount(CredentialForm, { props: { save } })
    const submit = wrapper.find('button[type="submit"]')
    expect(submit.attributes('disabled')).toBeDefined()

    await wrapper.find('input[type="checkbox"]').setValue(true)
    expect(submit.attributes('disabled')).toBeUndefined()
  })

  it('sends the secret fields of the chosen kind, masks them, and wipes them after saving', async () => {
    const save = vi.fn<(input: Partial<CredentialInput>) => Promise<void>>(async () => {})
    const wrapper = mount(CredentialForm, { props: { save } })
    const text = wrapper.findAll('input.input')
    await text[0]!.setValue('customer')
    await text[1]!.setValue('customer')
    await text[2]!.setValue('cust01@acme.io')
    const password = wrapper.find('input[type="password"]')
    await password.setValue('Zx9!vK-unguessable')
    await wrapper.find('input[type="checkbox"]').setValue(true)

    // Not offered to password managers as the user's own login.
    expect(password.attributes('autocomplete')).toBe('off')
    expect(wrapper.find('form').attributes('autocomplete')).toBe('off')

    await wrapper.find('form').trigger('submit')
    await flushPromises()

    expect(save).toHaveBeenCalledWith({
      name: 'customer',
      role_label: 'customer',
      setup_instructions: null,
      kind: 'username_password',
      is_test_account: true,
      secret: { username: 'cust01@acme.io', password: 'Zx9!vK-unguessable' },
    })
    expect((wrapper.find('input[type="password"]').element as HTMLInputElement).value).toBe('')
    expect(wrapper.html()).not.toContain('Zx9!vK-unguessable')
  })

  it('never shows a stored secret and only sends one when a replacement is typed', async () => {
    const save = vi.fn<(input: Partial<CredentialInput>) => Promise<void>>(async () => {})
    const wrapper = mount(CredentialForm, { props: { credential: stored, save } })

    const password = wrapper.find('input[type="password"]').element as HTMLInputElement
    expect(password.value).toBe('')
    expect(password.placeholder).toBe('Unchanged')
    expect(wrapper.text()).toContain('cannot be displayed')

    await wrapper.find('form').trigger('submit')
    await flushPromises()
    expect(save.mock.calls[0]![0]).not.toHaveProperty('secret')
    expect(save.mock.calls[0]![0]).not.toHaveProperty('kind')

    const inputs = wrapper.findAll('.form-grid input')
    await inputs[2]!.setValue('cust01@acme.io')
    await wrapper.find('input[type="password"]').setValue('rotated-pw-123')
    await wrapper.find('form').trigger('submit')
    await flushPromises()
    expect(save.mock.calls[1]![0].secret).toEqual({
      username: 'cust01@acme.io',
      password: 'rotated-pw-123',
    })
  })

  it('keeps a typed secret out of the page when saving fails, and reports the error', async () => {
    const save = vi.fn(async () => {
      throw new ApiError({
        status: 422,
        code: 'invalid_secret_fields',
        message: 'A username_password credential needs exactly these fields: password, username.',
      })
    })
    const wrapper = mount(CredentialForm, { props: { save } })
    await wrapper.find('input[type="password"]').setValue('typed-secret-123')
    await wrapper.find('input[type="checkbox"]').setValue(true)
    await wrapper.find('form').trigger('submit')
    await flushPromises()

    expect(wrapper.find('[role="alert"]').text()).toContain('needs exactly these fields')
    expect(wrapper.html()).not.toContain('typed-secret-123')
  })

  it('stores only a reference when the secret is kept elsewhere', async () => {
    const save = vi.fn<(input: Partial<CredentialInput>) => Promise<void>>(async () => {})
    const wrapper = mount(CredentialForm, { props: { save } })
    await wrapper.findAll('select')[1]!.setValue('reference')
    expect(wrapper.find('input[type="password"]').exists()).toBe(false)

    await wrapper.find('input[placeholder^="env:"]').setValue('env:STAGING_PW')
    await wrapper.find('input[type="checkbox"]').setValue(true)
    await wrapper.find('form').trigger('submit')
    await flushPromises()

    const sent = save.mock.calls[0]![0]
    expect(sent.external_ref).toBe('env:STAGING_PW')
    expect(sent).not.toHaveProperty('secret')
  })
})

const environment: Environment = {
  id: 'e1',
  application_id: 'a1',
  name: 'staging',
  kind: 'staging',
  base_url: 'https://staging.acme.io',
  api_base_url: null,
  allowed_origins: [
    {
      origin: 'https://staging.acme.io',
      scheme: 'https',
      host: 'staging.acme.io',
      port: 443,
      purpose: 'app',
    },
    {
      origin: 'https://auth.acme.io',
      scheme: 'https',
      host: 'auth.acme.io',
      port: 443,
      purpose: 'auth_provider',
    },
  ],
  variables: [
    { name: 'REGION', value: 'in', secret_ref: null },
    { name: 'API_TOKEN', value: null, secret_ref: 'api-token' },
  ],
  auth_config: { method: 'form_login' },
  allowed_actions: ['navigate', 'read', 'click'],
  is_read_only: false,
  requires_approval: false,
  allow_private_network: false,
  status: 'active',
  credential_count: 1,
  created_at: '',
  updated_at: '',
}

describe('EnvironmentForm', () => {
  function mountForm(props: { environment?: Environment; isAdmin?: boolean } = {}) {
    const save = vi.fn<(input: EnvironmentInput) => Promise<void>>(async () => {})
    const wrapper = mount(EnvironmentForm, {
      props: { save, isAdmin: props.isAdmin ?? false, environment: props.environment },
    })
    return { wrapper, save }
  }

  it('round-trips variables and extra origins when editing', async () => {
    const { wrapper, save } = mountForm({ environment })
    const areas = wrapper.findAll('textarea')
    expect((areas[0]!.element as HTMLTextAreaElement).value).toBe('https://auth.acme.io')
    expect((areas.at(-1)!.element as HTMLTextAreaElement).value).toBe(
      'REGION=in\nAPI_TOKEN=@api-token',
    )

    await wrapper.find('form').trigger('submit')
    await flushPromises()

    const sent = save.mock.calls[0]![0]
    expect(sent.variables).toEqual([
      { name: 'REGION', value: 'in' },
      { name: 'API_TOKEN', secret_ref: 'api-token' },
    ])
    expect(sent.additional_origins).toEqual([
      { url: 'https://auth.acme.io', purpose: 'auth_provider' },
    ])
    expect(sent.allowed_actions).toEqual(['navigate', 'read', 'click'])
  })

  it('defaults production to read-only with approval and limits its actions', async () => {
    const { wrapper, save } = mountForm({ isAdmin: true })
    await wrapper.findAll('select')[0]!.setValue('production')
    await wrapper.findAll('input.input')[0]!.setValue('production')
    await wrapper.find('input[type="url"]').setValue('https://prod.acme.io')
    await wrapper.find('form').trigger('submit')
    await flushPromises()

    const sent = save.mock.calls[0]![0]
    expect(sent.is_read_only).toBe(true)
    expect(sent.requires_approval).toBe(true)
    expect(sent.allowed_actions).toEqual(['navigate', 'read', 'api_read'])
  })

  it('does not let non-admins submit production or enable private networks', async () => {
    const { wrapper } = mountForm({ isAdmin: false })
    const privateNetwork = wrapper.findAll('input[type="checkbox"]')[2]!
    expect(privateNetwork.attributes('disabled')).toBeDefined()

    await wrapper.findAll('select')[0]!.setValue('production')
    expect(wrapper.find('button[type="submit"]').attributes('disabled')).toBeDefined()
    expect(wrapper.text()).toContain('only be created or changed by an organization admin')
  })
})

describe('useFormErrors', () => {
  it('maps server field errors to form fields and keeps a general message', () => {
    const errors = useFormErrors()
    errors.capture(
      new ApiError({
        status: 422,
        code: 'address_not_allowed',
        message: 'The host internal.acme.io resolves to a private network address.',
        errors: [
          { field: 'base_url', code: 'address_not_allowed', message: 'not allowed' },
          { field: 'body.name', code: 'missing', message: 'Field required' },
          { field: 'additional_origins.0.url', code: 'domain_not_allowed', message: 'outside' },
        ],
      }),
    )
    expect(errors.fields).toEqual({
      base_url: 'not allowed',
      name: 'Field required',
      additional_origins: 'outside',
    })
    expect(errors.general.value).toContain('private network address')

    errors.clear()
    expect(errors.fields).toEqual({})
    expect(errors.general.value).toBeNull()
  })
})
