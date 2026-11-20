<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { RouterLink, useRouter } from 'vue-router'

import BaseButton from '@/components/ui/BaseButton.vue'
import BaseModal from '@/components/ui/BaseModal.vue'
import EmptyState from '@/components/ui/EmptyState.vue'
import ErrorState from '@/components/ui/ErrorState.vue'
import FormField from '@/components/ui/FormField.vue'
import LoadingState from '@/components/ui/LoadingState.vue'
import StatusBadge from '@/components/ui/StatusBadge.vue'
import { useFormErrors } from '@/composables/useFormErrors'
import { useAuthStore } from '@/stores/auth'
import { useProjectsStore } from '@/stores/projects'

const auth = useAuthStore()
const projects = useProjectsStore()
const router = useRouter()
const errors = useFormErrors()

const creating = ref(false)
const saving = ref(false)
const name = ref('')
const description = ref('')

function openCreate(): void {
  name.value = ''
  description.value = ''
  errors.clear()
  creating.value = true
}

async function submit(): Promise<void> {
  saving.value = true
  try {
    const project = await projects.create({
      name: name.value,
      description: description.value || null,
    })
    creating.value = false
    await router.push({ name: 'project', params: { projectId: project.id } })
  } catch (error) {
    errors.capture(error)
  } finally {
    saving.value = false
  }
}

onMounted(() => projects.load())
</script>

<template>
  <section class="stack stack--lg">
    <header class="cluster cluster--between">
      <div>
        <h1>Projects</h1>
        <p class="muted">Group the applications you test and the people who test them.</p>
      </div>
      <BaseButton v-if="auth.canContribute" @click="openCreate">New project</BaseButton>
    </header>

    <LoadingState v-if="projects.loading && !projects.loadedFor" label="Loading projects…" />
    <ErrorState
      v-else-if="projects.error"
      :message="projects.error"
      retryable
      :retrying="projects.loading"
      @retry="projects.load()"
    />
    <EmptyState
      v-else-if="projects.items.length === 0"
      title="No projects yet"
      :message="
        auth.canContribute
          ? 'Create a project to register the first application you want to test.'
          : 'You have not been added to a project. Ask a project manager or an admin.'
      "
    >
      <BaseButton v-if="auth.canContribute" @click="openCreate">New project</BaseButton>
    </EmptyState>

    <div v-else class="grid">
      <RouterLink
        v-for="project in projects.items"
        :key="project.id"
        class="card card--link stack"
        :to="{ name: 'project', params: { projectId: project.id } }"
      >
        <div class="cluster cluster--between">
          <h2>{{ project.name }}</h2>
          <StatusBadge v-if="project.status === 'archived'" tone="neutral">Archived</StatusBadge>
        </div>
        <p v-if="project.description" class="small muted">{{ project.description }}</p>
        <p class="small muted">
          {{ project.application_count }}
          {{ project.application_count === 1 ? 'application' : 'applications' }} ·
          {{ project.member_count }} {{ project.member_count === 1 ? 'member' : 'members' }}
        </p>
      </RouterLink>
    </div>

    <BaseModal :open="creating" title="New project" @close="creating = false">
      <form class="stack" novalidate @submit.prevent="submit">
        <p v-if="errors.general.value" class="notice notice--danger" role="alert">
          {{ errors.general.value }}
        </p>
        <FormField v-slot="field" label="Name" :error="errors.fields.name">
          <input :id="field.id" v-model.trim="name" class="input" maxlength="120" required />
        </FormField>
        <FormField v-slot="field" label="Description" :error="errors.fields.description">
          <textarea :id="field.id" v-model.trim="description" class="input" maxlength="2000" />
        </FormField>
        <div class="cluster">
          <BaseButton type="submit" :loading="saving">Create project</BaseButton>
          <BaseButton variant="ghost" @click="creating = false">Cancel</BaseButton>
        </div>
      </form>
    </BaseModal>
  </section>
</template>
