const SIGNAL_LAYERS = ['F.Cu', 'B.Cu']

function netClass(overrides = {}) {
  return {
    name: 'General signals',
    kind: 'signal',
    nets: [],
    allowed_layers: [...SIGNAL_LAYERS],
    max_layer_transitions: 4,
    max_vias_per_net: 4,
    pullups_required: false,
    pullup_voltage_v: null,
    controlled_impedance: false,
    impedance_ohms: null,
    min_trace_width_mm: null,
    max_length_mm: null,
    max_skew_mm: null,
    max_frequency_hz: null,
    concerns: ['Confirm critical net names before layout'],
    ...overrides,
  }
}

export function constraintKinds(intent) {
  const text = String(intent || '').toLowerCase()
  const kinds = []
  if (/\bi2c\b|\bsda\b|\bscl\b/.test(text)) kinds.push('i2c')
  if (/\busb\b|\bd\+\b|\bd-\b/.test(text)) kinds.push('usb')
  if (/\beth(?:ernet)?\b|\brgmii\b|\brmii\b/.test(text)) kinds.push('ethernet')
  if (/\bspi\b|\bsclk\b|\bmosi\b|\bmiso\b/.test(text)) kinds.push('spi')
  return kinds.length ? kinds : ['signal']
}

export function suggestConstraintManifest(intent) {
  const classes = constraintKinds(intent).map((kind) => {
    if (kind === 'i2c') {
      return netClass({
        name: 'I2C bus',
        kind,
        nets: ['SDA', 'SCL'],
        max_layer_transitions: 2,
        max_vias_per_net: 2,
        pullups_required: true,
        pullup_voltage_v: 3.3,
        max_frequency_hz: 400000,
        concerns: ['Pull-ups', 'rise time', 'bus capacitance'],
      })
    }
    if (kind === 'usb') {
      return netClass({
        name: 'USB differential pair',
        kind,
        nets: ['USB_D+', 'USB_D-'],
        max_layer_transitions: 2,
        max_vias_per_net: 2,
        controlled_impedance: true,
        impedance_ohms: 90,
        max_skew_mm: 0.5,
        concerns: ['Differential impedance', 'pair skew', 'reference plane continuity'],
      })
    }
    if (kind === 'ethernet') {
      return netClass({
        name: 'Ethernet differential pairs',
        kind,
        nets: ['TX+', 'TX-', 'RX+', 'RX-'],
        max_layer_transitions: 2,
        max_vias_per_net: 2,
        controlled_impedance: true,
        impedance_ohms: 100,
        max_skew_mm: 0.5,
        concerns: ['Differential impedance', 'pair skew', 'magnetics placement'],
      })
    }
    if (kind === 'spi') {
      return netClass({
        name: 'SPI bus',
        kind,
        nets: ['SCLK', 'MOSI', 'MISO', 'CS'],
        max_layer_transitions: 2,
        max_vias_per_net: 2,
        concerns: ['Clock return path', 'series damping', 'length at target frequency'],
      })
    }
    return netClass()
  })
  return { version: 1, approved: false, board_layers: 2, net_classes: classes }
}

function finiteOrNull(value) {
  if (value === '' || value === null || value === undefined) return null
  const number = Number(value)
  return Number.isFinite(number) ? number : null
}

export function normalizeConstraintManifest(manifest = {}) {
  return {
    version: 1,
    approved: manifest.approved === true,
    board_layers: Math.max(1, Math.min(32, Math.round(Number(manifest.board_layers) || 2))),
    net_classes: (Array.isArray(manifest.net_classes) ? manifest.net_classes : []).map(
      (item) => ({
        name: String(item.name || '').trim(),
        kind: String(item.kind || 'signal').trim(),
        nets: (Array.isArray(item.nets) ? item.nets : String(item.nets || '').split(','))
          .map((net) => String(net).trim())
          .filter(Boolean),
        allowed_layers: (
          Array.isArray(item.allowed_layers)
            ? item.allowed_layers
            : String(item.allowed_layers || '').split(',')
        )
          .map((layer) => String(layer).trim())
          .filter(Boolean),
        max_layer_transitions: Math.max(0, Math.round(Number(item.max_layer_transitions) || 0)),
        max_vias_per_net: Math.max(0, Math.round(Number(item.max_vias_per_net) || 0)),
        pullups_required: item.pullups_required === true,
        pullup_voltage_v: finiteOrNull(item.pullup_voltage_v),
        controlled_impedance: item.controlled_impedance === true,
        impedance_ohms: finiteOrNull(item.impedance_ohms),
        min_trace_width_mm: finiteOrNull(item.min_trace_width_mm),
        max_length_mm: finiteOrNull(item.max_length_mm),
        max_skew_mm: finiteOrNull(item.max_skew_mm),
        max_frequency_hz: finiteOrNull(item.max_frequency_hz),
        concerns: (Array.isArray(item.concerns) ? item.concerns : [])
          .map((concern) => String(concern).trim())
          .filter(Boolean),
      }),
    ),
  }
}

export function constraintManifestReady(manifest = {}) {
  const classes = Array.isArray(manifest.net_classes) ? manifest.net_classes : []
  if (!classes.length) return false
  return classes.every((item) => {
    const nets = Array.isArray(item.nets) ? item.nets.filter(Boolean) : []
    const layers = Array.isArray(item.allowed_layers) ? item.allowed_layers.filter(Boolean) : []
    if (!String(item.name || '').trim() || !nets.length || !layers.length) return false
    if (!Number.isInteger(Number(item.max_layer_transitions))) return false
    if (!Number.isInteger(Number(item.max_vias_per_net))) return false
    if (item.pullups_required && !Number.isFinite(Number(item.pullup_voltage_v))) return false
    if (item.controlled_impedance && !Number.isFinite(Number(item.impedance_ohms))) return false
    return true
  })
}
