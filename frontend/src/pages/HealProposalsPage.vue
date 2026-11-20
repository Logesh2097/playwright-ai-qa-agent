<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { RouterLink } from 'vue-router'

import { healingApi } from '@/api/admin'
import BaseButton from '@/components/ui/BaseButton.vue'
import EmptyState from '@/components/ui/EmptyState.vue'
import ErrorState from '@/components/ui/ErrorState.vue'
import LoadingState from '@/components/ui/LoadingState.vue'
import StatusBadge from '@/components/ui/StatusBadge.vue'
import { useAsync } from '@/composables/useAsync'
import { toUserMessage } from '@/errors/messages'
import { dateTime, percent, showValue } from '@/pages/reportFormat'
import { useAuthStore } from '@/stores/auth'
import { useNotificationsStore } from '@/stores/notifications'
import type { HealProposal } from '@/types/admin'

const props = defineProps<{ projectId: string }>()
const auth = useAuthStore()
const notifications = useNotificationsStore()
const org = computed(() => auth.organizationId ?? '')
const { data, error, loading, run } = useAsync(() => healingApi.list(org.value, props.projectId))
const busy = ref<string | null>(null)

const open = computed(() =>
  (data.value ?? []).filter((p) => ['proposed', 'verified'].includes(p.status)),
)
const decided = computed(() =>
  (data.value ?? []).filter((p) => !['proposed', 'verified'].includes(p.status)),
)

async function decide(proposal: HealProposal, decision: 'accept' | 'reject'): Promise<void> {
  busy.value = proposal.id
  try {
    const answer = await healingApi.decide(org.value, props.projectId, proposal.id, decision)
    notifications.success(
      decision === 'accept'
        ? `Replacement accepted. The test case is now "${answer.case_status}" and has to be approved before the new locator is used.`
        : 'Replacement rejected. The test case is unchanged.',
    )
    await run()
  } catch (cause) {
    notifications.error(toUserMessage(cause))
  } finally {
    busy.value = null
  }
}

onMounted(run)
</script>

<template>
  <LoadingState v-if="loading && !data" label="Loading proposals…" />
  <ErrorState v-else-if="error" :message="error" />
  <section v-else class="stack stack--lg">
    <header class="stack">
      <RouterLink class="small" :to="{ name: 'project', params: { projectId } }"
        >← Project</RouterLink
      >
      <h1>Replacement locators</h1>
      <p class="muted">
        When a test could not find an element, the engine may have found it under a changed name.
        Accepting a replacement changes only that locator, as a new version of the test case that
        still has to be approved.
      </p>
    </header>

    <EmptyState
      v-if="open.length === 0"
      title="Nothing to decide"
      message="No replacement is waiting for a decision."
    />
    <article v-for="proposal in open" :key="proposal.id" class="card stack">
      <div class="cluster cluster--between">
        <strong>{{ proposal.action }} · step {{ (proposal.script_index ?? 0) + 1 }}</strong>
        <StatusBadge :tone="proposal.verified_by_rerun ? 'warning' : 'neutral'">
          {{ proposal.verified_by_rerun ? 'used in the run, step then passed' : 'found, not used' }}
        </StatusBadge>
      </div>
      <dl class="pair">
        <div>
          <dt>Locator in the script</dt>
          <dd class="mono">{{ showValue(proposal.old_locator) }}</dd>
        </div>
        <div>
          <dt>Replacement</dt>
          <dd class="mono">{{ showValue(proposal.new_locator) }}</dd>
        </div>
      </dl>
      <p class="small">{{ proposal.rationale }} Similarity {{ percent(proposal.confidence) }}.</p>
      <p v-if="proposal.held_back" class="notice">
        Not used automatically: {{ proposal.held_back }}
      </p>
      <div class="cluster cluster--between">
        <span class="small muted">{{ dateTime(proposal.created_at) }}</span>
        <div v-if="auth.canContribute" class="cluster">
          <BaseButton :loading="busy === proposal.id" @click="decide(proposal, 'accept')">
            Accept
          </BaseButton>
          <BaseButton
            variant="ghost"
            :disabled="busy === proposal.id"
            @click="decide(proposal, 'reject')"
          >
            Reject
          </BaseButton>
          <RouterLink
            v-if="proposal.test_case_id"
            class="small"
            :to="{ name: 'case', params: { projectId, caseId: proposal.test_case_id } }"
          >
            Open test case
          </RouterLink>
        </div>
      </div>
    </article>

    <section v-if="decided.length > 0" class="card stack">
      <h2>Decided</h2>
      <ul class="list">
        <li v-for="proposal in decided" :key="proposal.id">
          <span class="grow mono">{{ showValue(proposal.new_locator) }}</span>
          <span class="small muted">{{ dateTime(proposal.created_at) }}</span>
          <StatusBadge :tone="proposal.status === 'accepted' ? 'success' : 'neutral'">
            {{ proposal.status }}
          </StatusBadge>
        </li>
      </ul>
    </section>
  </section>
</template>

<style scoped>
.pair {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(16rem, 1fr));
  gap: var(--space-4);
  margin: 0;
}

.pair dt {
  color: var(--color-text-muted);
  font-size: var(--text-xs);
  font-weight: 600;
}

.pair dd {
  margin: 0;
}
</style>
