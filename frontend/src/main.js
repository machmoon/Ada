import { mount } from 'svelte'

// Self-hosted so the demo survives bad wifi — no Google Fonts request.
import '@fontsource/chivo/300.css'
import '@fontsource/chivo/400.css'
import '@fontsource/chivo/500.css'
import '@fontsource/chivo/600.css'
import '@fontsource/chivo/700.css'
import '@fontsource/chivo-mono/400.css'
import '@fontsource/chivo-mono/500.css'
import '@fontsource/chivo-mono/600.css'

import './styles/tokens.css'
import './styles/base.css'
import App from './App.svelte'

export default mount(App, { target: document.getElementById('app') })
