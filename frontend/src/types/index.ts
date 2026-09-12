/**
 * Core type definitions for RoomMind frontend.
 */

export type ClimateMode = "auto" | "heat_only" | "cool_only";

export type RoomMode = "idle" | "heating" | "cooling";

export type OverrideType = "boost" | "eco" | "custom";

export interface NotificationTarget {
  entity_id: string;
  person_entity: string;
  notify_when: "always" | "home_only";
}

export interface ScheduleEntry {
  entity_id: string;
}

export interface CoverScheduleEntry {
  entity_id: string;
  mode?: "force" | "gate";
}

export interface RoomLiveData {
  current_temp: number | null;
  current_humidity: number | null;
  target_temp: number | null;
  heat_target: number | null;
  cool_target: number | null;
  mode: RoomMode;
  heating_power: number; // 0-100
  device_setpoint: number | null; // Device target temp in Full Control mode
  override_active: boolean;
  override_type: OverrideType | null;
  override_heat: number | null;
  override_cool: number | null;
  override_until: number | null;
  override_suppressed: boolean;
  active_schedule_index: number;
  window_open: boolean;
  confidence: number | null;
  mpc_active: boolean;
  presence_away: boolean;
  mold_risk_level: "ok" | "warning" | "critical";
  mold_surface_rh: number | null;
  mold_prevention_active: boolean;
  mold_prevention_delta: number;
  blind_position: number | null;
  cover_auto_paused: boolean;
  cover_override_until?: number | null;
  cover_forced_reason: string;
  active_cover_schedule_index: number;
  active_heat_sources: string | null;
  learning_paused_reason: "outdoor_unavailable" | null;
  compressor_protection_active: boolean;
  compressor_protection_reason: "min_off" | "min_run" | null;
  coil_dry_active: boolean;
  coil_dry_phase: "drain" | "blow" | null;
  coil_dry_until: number | null;
  coil_dry_entities: string[];
  schedule_temp_warnings: ScheduleTempWarning[];
}

/** A schedule block temperature the backend discarded as implausible (#395). */
export interface ScheduleTempWarning {
  day: string; // lowercase English weekday, e.g. "monday"
  from: string; // "HH:MM:SS"
  field: "temperature" | "heat_temperature" | "cool_temperature";
  value: string; // the offending raw value; may not be numeric
}

export type DeviceType = "trv" | "ac";
export type DeviceRole = "primary" | "secondary" | "auto";

export interface DeviceConfig {
  entity_id: string;
  type: DeviceType;
  role: DeviceRole;
  heating_system_type?: string;
  idle_action?: "off" | "fan_only" | "setback" | "low"; // default "off"
  idle_fan_mode?: string; // default "low"
  setpoint_mode?: "proportional" | "direct"; // default "proportional"
  coil_dry?: "inherit" | "on" | "off"; // default "inherit"
  coil_dry_minutes?: number; // 0 = inherit global
  coil_dry_mode?: "" | "fan_only" | "dry"; // "" = inherit global
  coil_dry_fan_mode?: string; // "" = inherit global, "__keep__" = keep current
}

export type ConflictResolution =
  | "heating_priority"
  | "cooling_priority"
  | "majority"
  | "outdoor_temp";

export interface CompressorGroup {
  id: string;
  name: string;
  members: string[];
  min_run_minutes: number;
  min_off_minutes: number;
  master_entity: string;
  conflict_resolution: ConflictResolution;
  action_script: string;
  enforce_uniform_mode: boolean;
}

export interface RoomConfig {
  area_id: string;
  thermostats: string[];
  acs: string[];
  devices?: DeviceConfig[];
  temperature_sensor: string;
  humidity_sensor: string;
  occupancy_sensors?: string[];
  window_sensors: string[];
  window_open_delay: number;
  window_close_delay: number;
  climate_mode: ClimateMode;
  schedules: ScheduleEntry[];
  schedule_selector_entity: string;
  comfort_temp?: number;
  eco_temp?: number;
  comfort_heat: number;
  comfort_cool: number;
  comfort_heat_entity?: string;
  eco_heat: number;
  eco_cool: number;
  override_heat?: number | null;
  override_cool?: number | null;
  override_until?: number | null;
  override_type?: OverrideType | null;
  presence_persons?: string[];
  display_name?: string;
  heating_system_type?: string;
  covers?: string[];
  covers_auto_enabled?: boolean;
  covers_deploy_threshold?: number;
  covers_min_position?: number;
  covers_override_minutes?: number;
  cover_schedules?: CoverScheduleEntry[];
  cover_schedule_selector_entity?: string;
  cover_orientations?: Record<string, number>;
  covers_outdoor_min_temp?: number | null;
  covers_night_close?: boolean;
  covers_night_position?: number;
  covers_night_close_elevation?: number;
  covers_night_close_offset_minutes?: number;
  covers_snap_deploy?: boolean;
  cover_min_positions?: Record<string, number>;
  ignore_presence?: boolean;
  is_outdoor?: boolean;
  valve_protection_exclude?: string[];
  heat_source_orchestration?: boolean;
  heat_source_primary_delta?: number;
  heat_source_outdoor_threshold?: number;
  heat_source_ac_min_outdoor?: number;
  climate_control_enabled?: boolean;
  live?: RoomLiveData;
}

export interface GlobalSettings {
  outdoor_temp_sensor: string;
  outdoor_humidity_sensor: string;
  outdoor_cooling_min?: number;
  outdoor_heating_max?: number;
  control_mode?: "mpc" | "bangbang";
  comfort_weight?: number;
  weather_entity?: string;
  outdoor_unavailable_notify?: boolean;
  climate_control_active?: boolean;
  learning_disabled_rooms?: string[];
  hidden_rooms?: string[];
  prediction_enabled?: boolean;
  vacation_temp?: number;
  vacation_until?: number | null;
  presence_enabled?: boolean;
  presence_persons?: string[];
  presence_away_action?: "eco" | "off";
  presence_clears_override?: boolean;
  schedule_off_action?: "eco" | "off";
  valve_protection_enabled?: boolean;
  valve_protection_interval_days?: number;
  coil_dry_enabled?: boolean;
  coil_dry_minutes?: number;
  coil_dry_mode?: "fan_only" | "dry";
  coil_dry_fan_mode?: string;
  coil_dry_min_cooling_minutes?: number;
  coil_dry_drain_minutes?: number;
  mold_detection_enabled?: boolean;
  mold_humidity_threshold?: number;
  mold_sustained_minutes?: number;
  mold_notification_cooldown?: number;
  mold_notifications_enabled?: boolean;
  mold_notification_targets?: NotificationTarget[];
  mold_prevention_enabled?: boolean;
  mold_prevention_intensity?: "light" | "medium" | "strong";
  mold_prevention_notify_enabled?: boolean;
  mold_prevention_notify_targets?: NotificationTarget[];
  compressor_groups?: CompressorGroup[];
  room_order?: string[];
  group_by_floor?: boolean;
  boost_applied_at?: Record<string, number>;
}

// HA types for panel integration
export interface HassConnection {
  addEventListener(event: string, callback: () => void): void;
  removeEventListener(event: string, callback: () => void): void;
}

export interface HomeAssistant {
  callWS: <T>(msg: Record<string, unknown>) => Promise<T>;
  callService: (domain: string, service: string, data?: Record<string, unknown>) => Promise<void>;
  states: Record<string, HassEntity>;
  areas: Record<string, HassArea>;
  floors?: Record<string, HassFloor>;
  entities: Record<string, HassEntityRegistryEntry>;
  devices: Record<string, HassDeviceRegistryEntry>;
  language: string;
  config: { unit_system: { temperature: string } };
  connection?: HassConnection;
}

export interface HassArea {
  area_id: string;
  name: string;
  picture: string | null;
  floor_id: string | null;
}

export interface HassFloor {
  floor_id: string;
  name: string;
  level: number | null;
}

export interface HassEntityRegistryEntry {
  entity_id: string;
  area_id: string | null;
  device_id: string | null;
  platform: string;
}

export interface HassDeviceRegistryEntry {
  id: string;
  area_id: string | null;
}

export interface HassEntity {
  entity_id: string;
  state: string;
  attributes: Record<string, unknown>;
}

export interface AnalyticsDataPoint {
  ts: number;
  room_temp: number | null;
  outdoor_temp: number | null;
  target_temp: number | null;
  mode: string;
  predicted_temp: number | null;
  window_open: boolean;
  heating_power: number | null;
  solar_irradiance: number | null;
  blind_position?: number | null;
  cover_reason?: string;
  device_setpoint?: number | null;
}

export interface AnalyticsData {
  detail: AnalyticsDataPoint[];
  history: AnalyticsDataPoint[];
  forecast?: AnalyticsDataPoint[];
  model: {
    confidence: number;
    model: {
      C: number;
      U: number;
      Q_heat: number;
      Q_cool: number;
      Q_solar: number;
      Q_occupancy: number;
    };
    n_samples: number;
    n_observations: number;
    n_heating: number;
    n_cooling: number;
    applicable_modes: string[];
    mpc_active: boolean;
    sigma_e: number;
    prediction_std_idle: number;
    prediction_std_heating: number;
    has_occupancy_sensors: boolean;
  };
}

export type TimeRange = "12h" | "24h" | "2d" | "7d" | "14d" | "30d" | "90d";
