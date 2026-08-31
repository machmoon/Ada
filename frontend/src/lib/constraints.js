const SIGNAL_LAYERS = ['F.Cu', 'B.Cu']

function netClass(overrides = {}) {
  return {
    name: 'General signals',
    kind: 'signal',
    nets: [],
    allowed_layers: [...SIGNAL_LAYERS],
    max_layer_transitions: 4,
    max_vias_per_net: 4,
    signal_voltage_v: null,
    max_frequency_hz: null,
    pullups_required: false,
    pullup_rail: null,
    pullup_min_ohms: null,
    pullup_max_ohms: null,
    bus_capacitance_pf: null,
    max_rise_time_ns: null,
    controlled_impedance: false,
    impedance_ohms: null,
    impedance_tolerance_percent: null,
    pair_spacing_mm: null,
    reference_plane: null,
    min_trace_width_mm: null,
    max_length_mm: null,
    max_skew_mm: null,
    max_stub_length_mm: null,
    expected_current_a: null,
    copper_weight_oz: null,
    max_voltage_drop_v: null,
    min_separation_mm: null,
    min_thermal_separation_mm: null,
    guard_required: false,
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
  if (/\bclock\b|\bclk\b|\boscillator\b|\bcrystal\b/.test(text)) kinds.push('clock')
  if (/\bpower\b|\bmotor\b|\bregulator\b|\bcurrent\b|\bvin\b|\bvout\b/.test(text)) kinds.push('power')
  if (/\banalog\b|\badc\b|\bdac\b|\bsensor input\b/.test(text)) kinds.push('analog')
  if (/\brf\b|\bantenna\b|\bwireless\b|\b2\.4\s*ghz\b/.test(text)) kinds.push('rf')
  return kinds.length ? [...new Set(kinds)] : ['signal']
}

function template(kind) {
  const templates = {
    i2c: {
      name: 'I2C bus', nets: ['SDA', 'SCL'], max_layer_transitions: 2,
      max_vias_per_net: 2, signal_voltage_v: 3.3, max_frequency_hz: 400000,
      pullups_required: true, pullup_rail: '3V3', pullup_min_ohms: 1000,
      pullup_max_ohms: 10000, bus_capacitance_pf: 50, max_rise_time_ns: 300,
      concerns: ['Pull-ups', 'rise time', 'bus capacitance'],
    },
    usb: {
      name: 'USB differential pair', nets: ['USB_D+', 'USB_D-'],
      max_layer_transitions: 2, max_vias_per_net: 2, controlled_impedance: true,
      impedance_ohms: 90, impedance_tolerance_percent: 10, pair_spacing_mm: 0.2,
      reference_plane: 'GND', min_trace_width_mm: 0.2, max_skew_mm: 0.5,
      concerns: ['Differential impedance', 'pair skew', 'reference plane continuity'],
    },
    ethernet: {
      name: 'Ethernet differential pairs', nets: ['TX+', 'TX-', 'RX+', 'RX-'],
      max_layer_transitions: 2, max_vias_per_net: 2, controlled_impedance: true,
      impedance_ohms: 100, impedance_tolerance_percent: 10, pair_spacing_mm: 0.2,
      reference_plane: 'GND', min_trace_width_mm: 0.2, max_skew_mm: 0.5,
      concerns: ['Differential impedance', 'pair skew', 'magnetics placement'],
    },
    spi: {
      name: 'Fast SPI bus', nets: ['SCLK', 'MOSI', 'MISO', 'CS'],
      max_layer_transitions: 2, max_vias_per_net: 2, max_frequency_hz: 10000000,
      max_length_mm: 50, max_skew_mm: 5, max_stub_length_mm: 5,
      reference_plane: 'GND', min_trace_width_mm: 0.2,
      concerns: ['Clock return path', 'length', 'skew', 'stubs'],
    },
    clock: {
      name: 'Clock', nets: ['CLK'], max_layer_transitions: 2, max_vias_per_net: 2,
      max_frequency_hz: 25000000, max_length_mm: 50, max_skew_mm: 2,
      max_stub_length_mm: 2, reference_plane: 'GND', min_trace_width_mm: 0.2,
      concerns: ['Return path', 'length', 'stubs'],
    },
    power: {
      name: 'Power distribution', nets: ['VIN', 'VOUT', 'GND'],
      max_layer_transitions: 4, max_vias_per_net: 4, expected_current_a: 1,
      min_trace_width_mm: 0.5, copper_weight_oz: 1, max_voltage_drop_v: 0.1,
      min_thermal_separation_mm: 5,
      concerns: ['Expected current', 'trace width', 'voltage drop', 'thermal path'],
    },
    analog: {
      name: 'Sensitive analog', nets: ['ADC_IN', 'AGND'], max_layer_transitions: 2,
      max_vias_per_net: 2, min_trace_width_mm: 0.2, min_separation_mm: 0.5,
      reference_plane: 'AGND', guard_required: true,
      concerns: ['Isolation', 'guard region', 'return path'],
    },
    rf: {
      name: 'RF path', nets: ['RF_IN', 'RF_OUT'], max_layer_transitions: 1,
      max_vias_per_net: 1, controlled_impedance: true, impedance_ohms: 50,
      impedance_tolerance_percent: 10, pair_spacing_mm: 0.5, reference_plane: 'GND',
      min_trace_width_mm: 0.2, max_length_mm: 30, max_skew_mm: 1,
      min_separation_mm: 1, guard_required: true,
      concerns: ['Impedance', 'isolation', 'return path', 'keepouts'],
    },
  }
  return netClass({ kind, ...(templates[kind] || {}) })
}

export function suggestConstraintManifest(intent) {
  return {
    version: 2,
    approved: false,
    board_layers: 2,
    net_classes: constraintKinds(intent).map(template),
    mechanical: {
      max_board_width_mm: null,
      max_board_height_mm: null,
      max_component_height_mm: null,
      mounting_hole_refs: [],
      keepouts: [],
      fixed_placements: [],
    },
    soft_preferences: {
      fewer_vias: 1,
      shorter_traces: 0.1,
      compact_grouping: 0.01,
      thermal_separation: 1,
      connector_accessibility: 1,
    },
  }
}

function finiteOrNull(value) {
  if (value === '' || value === null || value === undefined) return null
  const number = Number(value)
  return Number.isFinite(number) ? number : null
}

function strings(value) {
  return (Array.isArray(value) ? value : String(value || '').split(','))
    .map((item) => String(item).trim())
    .filter(Boolean)
}

function normalizeNetClass(item) {
  const numbers = [
    'signal_voltage_v', 'max_frequency_hz', 'pullup_min_ohms', 'pullup_max_ohms',
    'bus_capacitance_pf', 'max_rise_time_ns', 'impedance_ohms',
    'impedance_tolerance_percent', 'pair_spacing_mm', 'min_trace_width_mm',
    'max_length_mm', 'max_skew_mm', 'max_stub_length_mm', 'expected_current_a',
    'copper_weight_oz', 'max_voltage_drop_v', 'min_separation_mm',
    'min_thermal_separation_mm',
  ]
  const normalized = {
    ...netClass(),
    name: String(item.name || '').trim(),
    kind: String(item.kind || 'signal').trim(),
    nets: strings(item.nets),
    allowed_layers: strings(item.allowed_layers),
    max_layer_transitions: Math.max(0, Math.round(Number(item.max_layer_transitions) || 0)),
    max_vias_per_net: Math.max(0, Math.round(Number(item.max_vias_per_net) || 0)),
    pullups_required: item.pullups_required === true,
    pullup_rail: String(item.pullup_rail || '').trim() || null,
    controlled_impedance: item.controlled_impedance === true,
    reference_plane: String(item.reference_plane || '').trim() || null,
    guard_required: item.guard_required === true,
    concerns: strings(item.concerns),
  }
  for (const name of numbers) normalized[name] = finiteOrNull(item[name])
  return normalized
}

export function normalizeConstraintManifest(manifest = {}) {
  const mechanical = manifest.mechanical || {}
  const soft = manifest.soft_preferences || {}
  return {
    version: 2,
    approved: manifest.approved === true,
    board_layers: Math.max(1, Math.min(32, Math.round(Number(manifest.board_layers) || 2))),
    net_classes: (Array.isArray(manifest.net_classes) ? manifest.net_classes : []).map(normalizeNetClass),
    mechanical: {
      max_board_width_mm: finiteOrNull(mechanical.max_board_width_mm),
      max_board_height_mm: finiteOrNull(mechanical.max_board_height_mm),
      max_component_height_mm: finiteOrNull(mechanical.max_component_height_mm),
      mounting_hole_refs: strings(mechanical.mounting_hole_refs),
      keepouts: Array.isArray(mechanical.keepouts) ? mechanical.keepouts : [],
      fixed_placements: Array.isArray(mechanical.fixed_placements) ? mechanical.fixed_placements : [],
    },
    soft_preferences: Object.fromEntries(
      ['fewer_vias', 'shorter_traces', 'compact_grouping', 'thermal_separation', 'connector_accessibility']
        .map((name) => [name, finiteOrNull(soft[name]) || 0]),
    ),
  }
}

function hasNumbers(item, names) {
  return names.every((name) => Number.isFinite(Number(item[name])))
}

function netClassReady(item) {
  if (!String(item.name || '').trim() || !strings(item.nets).length || !strings(item.allowed_layers).length) return false
  if (!Number.isInteger(Number(item.max_layer_transitions)) || !Number.isInteger(Number(item.max_vias_per_net))) return false
  if (item.pullups_required && (!item.pullup_rail || !hasNumbers(item, ['pullup_min_ohms', 'pullup_max_ohms']))) return false
  if (item.controlled_impedance && (!item.reference_plane || !hasNumbers(item, ['impedance_ohms', 'impedance_tolerance_percent', 'pair_spacing_mm', 'max_skew_mm']))) return false
  const required = {
    i2c: ['signal_voltage_v', 'max_frequency_hz', 'bus_capacitance_pf', 'max_rise_time_ns'],
    spi: ['max_frequency_hz', 'max_length_mm', 'max_skew_mm'],
    clock: ['max_frequency_hz', 'max_length_mm', 'max_skew_mm'],
    power: ['expected_current_a', 'min_trace_width_mm', 'copper_weight_oz', 'max_voltage_drop_v', 'min_thermal_separation_mm'],
    analog: ['min_separation_mm'],
    rf: ['min_separation_mm', 'max_length_mm'],
  }
  if (!hasNumbers(item, required[item.kind] || [])) return false
  return !['spi', 'clock', 'analog', 'rf'].includes(item.kind) || Boolean(item.reference_plane)
}

export function constraintManifestReady(manifest = {}) {
  const classes = Array.isArray(manifest.net_classes) ? manifest.net_classes : []
  return classes.length > 0 && classes.every(netClassReady)
}
