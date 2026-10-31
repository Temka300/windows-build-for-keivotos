import { mount } from 'svelte'
import './app.css'
import App from './App.svelte'
import { installDiagnostics } from './lib/diagnostics'

installDiagnostics()

const app = mount(App, {
  target: document.getElementById('app')!,
})

export default app
