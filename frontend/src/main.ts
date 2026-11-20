import './styles/base.css'

import { createPinia } from 'pinia'
import { createApp } from 'vue'

import App from './App.vue'
import { installGlobalErrorHandlers } from './errors/globalHandlers'
import { createAppRouter } from './router'

const app = createApp(App)
const router = createAppRouter()

app.use(createPinia())
app.use(router)
installGlobalErrorHandlers(app, router)

app.mount('#app')
