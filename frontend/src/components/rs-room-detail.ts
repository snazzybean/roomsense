import { LitElement, html, css, nothing } from "lit";
import { unsafeHTML } from "lit/directives/unsafe-html.js";
import { customElement, property, state } from "lit/decorators.js";
import type {
  HomeAssistant,
  HassArea,
  RoomConfig,
  ClimateMode,
  ScheduleEntry,
  CoverScheduleEntry,
  DeviceConfig,
  DeviceType,
  DeviceRole,
} from "../types";
import "./rs-hero-status";
import "./rs-climate-mode-selector";
import "./rs-schedule-settings";
import "./rs-device-section";
import "./rs-sensor-section";
import "./rs-section-card";
import "./rs-override-section";
import "./rs-presence-section";
import "./rs-covers-section";
import "./rs-heat-source-section";
import "../components/shared/rs-toggle-row";
import "../components/shared/rs-toggle-card";
import "../components/shared/rs-edit-dialog";
import "../components/shared/rs-info-icon";
import { localize } from "../utils/localize";
import { fireSaveStatus } from "../utils/events";
import { resolveHeatingSystemType } from "../utils/device-utils";
import type { RsOverrideSection } from "./rs-override-section";

const CONTROL_DOCS_URL =
  "https://github.com/snazzybean/roommind/blob/main/docs/control-and-devices.md";

type EditableSection = "schedule" | "devices" | "sensors" | "presence" | "covers" | "heatSource";

@customElement("rs-room-detail")
export class RsRoomDetail extends LitElement {
  @property({ attribute: false }) public area!: HassArea;
  @property({ attribute: false }) public config: RoomConfig | null = null;
  @property({ attribute: false }) public hass!: HomeAssistant;
  @property({ type: Boolean }) public presenceEnabled = false;
  @property({ attribute: false }) public presencePersons: string[] = [];
  @property({ type: Boolean }) public climateControlActive = true;

  @property({ type: Boolean }) public valveProtectionEnabled = false;
  @property({ type: Boolean }) public coilDryEnabled = false;
  @property({ type: Number }) public coilDryMinutes = 20;
  @property({ type: String }) public coilDryMode = "fan_only";
  @property({ type: String }) public coilDryFanMode = "low";

  @state() private _devices: DeviceConfig[] = [];
  @state() private _selectedTempSensor = "";
  @state() private _selectedHumiditySensor = "";
  @state() private _selectedOccupancySensors: Set<string> = new Set();
  @state() private _selectedWindowSensors: Set<string> = new Set();
  @state() private _windowOpenDelay = 0;
  @state() private _windowCloseDelay = 0;
  @state() private _climateMode: ClimateMode = "auto";
  @state() private _schedules: ScheduleEntry[] = [];
  @state() private _scheduleSelectorEntity = "";
  @state() private _comfortHeat = 21.0;
  @state() private _comfortCool = 24.0;
  @state() private _comfortHeatEntity = "";
  @state() private _ecoHeat = 17.0;
  @state() private _ecoCool = 27.0;
  @state() private _error = "";
  @state() private _dirty = false;
  @state() private _editing: EditableSection | null = null;
  @state() private _selectedPresencePersons: string[] = [];
  @state() private _displayName = "";
  @state() private _selectedCovers: Set<string> = new Set();
  @state() private _coversAutoEnabled = false;
  @state() private _coversDeployThreshold = 1.5;
  @state() private _coversMinPosition = 0;
  @state() private _coversOverrideMinutes = 60;
  @state() private _coverSchedules: CoverScheduleEntry[] = [];
  @state() private _coverScheduleSelectorEntity = "";
  @state() private _coversNightClose = false;
  @state() private _coversNightPosition = 0;
  @state() private _coversSnapDeploy = false;
  @state() private _coverOrientations: Record<string, number> = {};
  @state() private _coversNightCloseElevation = 0;
  @state() private _coversNightCloseOffsetMinutes = 0;
  @state() private _coversOutdoorMinTemp: number | null = 10;
  @state() private _coverMinPositions: Record<string, number> = {};
  @state() private _ignorePresence = false;
  @state() private _isOutdoor = false;
  @state() private _valveProtectionExclude: Set<string> = new Set();
  @state() private _climateControlEnabled = true;
  @state() private _heatSourceOrchestration = false;
  @state() private _heatSourcePrimaryDelta = 1.5;
  @state() private _heatSourceOutdoorThreshold = 5.0;
  @state() private _heatSourceAcMinOutdoor = -15.0;
  @state() private _optimisticCoverResume = false;

  private _prevAreaId: string | null = null;
  private _saveDebounce?: ReturnType<typeof setTimeout>;

  static styles = css`
    :host {
      display: block;
      max-width: 2400px;
      margin: 0 auto;
    }

    .detail-layout {
      display: flex;
      flex-direction: column;
      gap: 16px;
    }

    .detail-grid {
      column-count: 3;
      column-width: 360px;
      column-gap: 16px;
      column-fill: balance;
    }

    .detail-grid > * {
      display: block;
      width: 100%;
      break-inside: avoid;
      page-break-inside: avoid;
      margin-bottom: 16px;
    }

    @media (min-width: 1900px) {
      .detail-grid {
        column-count: 4;
      }
    }

    /* Section cards handled by rs-section-card */

    /* YAML code block for info panels (slotted into edit dialogs) */
    .yaml-block {
      background: var(--code-editor-background-color, rgba(0, 0, 0, 0.35));
      border: 1px solid var(--divider-color, rgba(255, 255, 255, 0.12));
      border-radius: 6px;
      padding: 10px 14px;
      margin: 8px 0;
      font-family: var(--code-font-family, monospace);
      font-size: 12px;
      line-height: 1.6;
      white-space: pre;
      overflow-x: auto;
      color: var(--primary-text-color);
    }
    .yaml-key {
      color: #82aaff;
    }
    .yaml-value {
      color: #e2a76a;
    }

    /* Actions */
    .actions {
      display: flex;
      align-items: center;
      gap: 12px;
      margin-top: 8px;
      margin-bottom: 24px;
    }

    .error {
      color: var(--error-color, #d32f2f);
      font-size: 13px;
      margin-top: 8px;
    }

    .field-hint {
      color: var(--secondary-text-color);
      font-size: 12px;
    }

    .exceptions-link {
      display: inline-flex;
      align-items: center;
      gap: 4px;
      background: none;
      border: none;
      padding: 8px 0 0;
      margin: 0;
      cursor: pointer;
      font-size: 13px;
      color: var(--primary-color);
      font-family: inherit;
    }

    .exceptions-link:hover {
      text-decoration: underline;
    }

    .helper-link {
      display: inline-block;
      margin-top: 12px;
      color: var(--primary-color);
      font-size: 12px;
      text-decoration: none;
    }

    .helper-link:hover {
      text-decoration: underline;
    }
  `;

  connectedCallback() {
    super.connectedCallback();
    this._initFromConfig();
  }

  disconnectedCallback() {
    super.disconnectedCallback();
    if (this._saveDebounce) clearTimeout(this._saveDebounce);
  }

  updated(changedProps: Map<string, unknown>) {
    const currentAreaId = this.config?.area_id ?? this.area?.area_id ?? null;
    const areaChanged = currentAreaId !== this._prevAreaId;

    if (areaChanged) {
      this._initFromConfig();
      this._prevAreaId = currentAreaId;
    } else if (changedProps.has("config") && !this._dirty) {
      const prevConfig = changedProps.get("config") as RoomConfig | null | undefined;
      if (prevConfig === null || prevConfig === undefined) {
        this._initFromConfig();
      } else if (this._comfortHeatEntity && this.config?.comfort_heat !== undefined) {
        // Comfort follows the source entity, so keep showing the device value —
        // otherwise the next save would push a stale one back onto the device.
        this._comfortHeat = this.config.comfort_heat;
      }
    }

    if (
      changedProps.has("config") &&
      this._optimisticCoverResume &&
      !this.config?.live?.cover_auto_paused
    ) {
      this._optimisticCoverResume = false;
    }
  }

  private _initFromConfig() {
    if (this.config) {
      if (this.config.devices?.length) {
        this._devices = [...this.config.devices];
      } else {
        this._devices = [
          ...(this.config.thermostats ?? []).map((eid) => ({
            entity_id: eid,
            type: "trv" as DeviceType,
            role: "auto" as DeviceRole,
            heating_system_type: this.config!.heating_system_type ?? "",
          })),
          ...(this.config.acs ?? []).map((eid) => ({
            entity_id: eid,
            type: "ac" as DeviceType,
            role: "auto" as DeviceRole,
          })),
        ];
      }
      this._selectedTempSensor = this.config.temperature_sensor;
      this._selectedHumiditySensor = this.config.humidity_sensor ?? "";
      this._selectedOccupancySensors = new Set(this.config.occupancy_sensors ?? []);
      this._selectedWindowSensors = new Set(this.config.window_sensors ?? []);
      this._windowOpenDelay = this.config.window_open_delay ?? 0;
      this._windowCloseDelay = this.config.window_close_delay ?? 0;
      this._climateMode = this.config.climate_mode;
      this._schedules = this.config.schedules ?? [];
      this._scheduleSelectorEntity = this.config.schedule_selector_entity ?? "";
      this._comfortHeat = this.config.comfort_heat ?? this.config.comfort_temp ?? 21.0;
      this._comfortCool = this.config.comfort_cool ?? 24.0;
      this._comfortHeatEntity = this.config.comfort_heat_entity ?? "";
      this._ecoHeat = this.config.eco_heat ?? this.config.eco_temp ?? 17.0;
      this._ecoCool = this.config.eco_cool ?? 27.0;
      this._selectedPresencePersons = this.config.presence_persons ?? [];
      this._displayName = this.config.display_name ?? "";
      this._selectedCovers = new Set(this.config.covers ?? []);
      this._coversAutoEnabled = this.config.covers_auto_enabled ?? false;
      this._coversDeployThreshold = this.config.covers_deploy_threshold ?? 1.5;
      this._coversMinPosition = this.config.covers_min_position ?? 0;
      this._coversOverrideMinutes = this.config.covers_override_minutes ?? 60;
      this._coverSchedules = this.config.cover_schedules ?? [];
      this._coverScheduleSelectorEntity = this.config.cover_schedule_selector_entity ?? "";
      this._coversNightClose = this.config.covers_night_close ?? false;
      this._coversNightPosition = this.config.covers_night_position ?? 0;
      this._coversSnapDeploy = this.config.covers_snap_deploy ?? false;
      this._coverOrientations = this.config.cover_orientations ?? {};
      this._coversNightCloseElevation = this.config.covers_night_close_elevation ?? 0;
      this._coversNightCloseOffsetMinutes = this.config.covers_night_close_offset_minutes ?? 0;
      this._coversOutdoorMinTemp = this.config.covers_outdoor_min_temp ?? 10;
      this._coverMinPositions = this.config.cover_min_positions ?? {};
      this._ignorePresence = this.config.ignore_presence ?? false;
      this._isOutdoor = this.config.is_outdoor ?? false;
      this._valveProtectionExclude = new Set(this.config.valve_protection_exclude ?? []);
      this._climateControlEnabled = this.config.climate_control_enabled ?? true;
      this._heatSourceOrchestration = this.config.heat_source_orchestration ?? false;
      this._heatSourcePrimaryDelta = this.config.heat_source_primary_delta ?? 1.5;
      this._heatSourceOutdoorThreshold = this.config.heat_source_outdoor_threshold ?? 5.0;
      this._heatSourceAcMinOutdoor = this.config.heat_source_ac_min_outdoor ?? -15.0;
    } else {
      this._devices = [];
      this._selectedTempSensor = "";
      this._selectedHumiditySensor = "";
      this._selectedOccupancySensors = new Set();
      this._selectedWindowSensors = new Set();
      this._windowOpenDelay = 0;
      this._windowCloseDelay = 0;
      this._climateMode = "auto";
      this._schedules = [];
      this._scheduleSelectorEntity = "";
      this._comfortHeat = 21.0;
      this._comfortCool = 24.0;
      this._comfortHeatEntity = "";
      this._ecoHeat = 17.0;
      this._ecoCool = 27.0;
      this._selectedPresencePersons = [];
      this._displayName = "";
      this._selectedCovers = new Set();
      this._coversAutoEnabled = false;
      this._coversDeployThreshold = 1.5;
      this._coversMinPosition = 0;
      this._coversOverrideMinutes = 60;
      this._coverSchedules = [];
      this._coverScheduleSelectorEntity = "";
      this._coversNightClose = false;
      this._coversNightPosition = 0;
      this._coversSnapDeploy = false;
      this._coverOrientations = {};
      this._coversNightCloseElevation = 0;
      this._coversNightCloseOffsetMinutes = 0;
      this._coversOutdoorMinTemp = 10;
      this._coverMinPositions = {};
      this._ignorePresence = false;
      this._isOutdoor = false;
      this._valveProtectionExclude = new Set();
      this._climateControlEnabled = true;
      this._heatSourceOrchestration = false;
      this._heatSourcePrimaryDelta = 1.5;
      this._heatSourceOutdoorThreshold = 5.0;
      this._heatSourceAcMinOutdoor = -15.0;
    }
    this._dirty = false;

    // Unconfigured rooms open the device-edit dialog automatically.
    if (this._devices.length === 0 && this._editing === null) {
      this._editing = "devices";
    }
  }

  private _openEdit = (section: EditableSection) => () => {
    this._editing = section;
  };

  private _closeEdit = () => {
    this._editing = null;
  };

  /** Expose effective override for hero-status via the override sub-component. */
  private _getEffectiveOverride(): {
    active: boolean;
    type: import("../types").OverrideType | null;
    heat: number | null;
    cool: number | null;
    until: number | null;
  } {
    const overrideEl = this.shadowRoot?.querySelector(
      "rs-override-section",
    ) as RsOverrideSection | null;
    if (overrideEl) {
      return overrideEl.getEffectiveOverride();
    }
    // Fallback before sub-component mounts
    const live = this.config?.live;
    if (live?.override_active && live.override_type) {
      return {
        active: true,
        type: live.override_type,
        heat: live.override_heat,
        cool: live.override_cool,
        until: live.override_until,
      };
    }
    return { active: false, type: null, heat: null, cool: null, until: null };
  }

  render() {
    if (!this.area) return nothing;

    return html`
      <div class="detail-layout">
        <rs-hero-status
          .hass=${this.hass}
          .area=${this.area}
          .config=${this.config}
          .isOutdoor=${this._isOutdoor}
          .overrideInfo=${this._getEffectiveOverride()}
          .climateControlActive=${this.climateControlActive && this._climateControlEnabled}
          @display-name-changed=${this._onDisplayNameChanged}
        ></rs-hero-status>

        <div class="detail-grid">
          ${!this._isOutdoor
            ? html`
                <rs-toggle-card
                  icon="mdi:power"
                  .label=${localize("room.climate_control_toggle", this.hass.language)}
                  .hint=${localize("room.climate_control_hint", this.hass.language)}
                  .checked=${this._climateControlEnabled}
                  @toggle-changed=${this._onClimateControlToggle}
                ></rs-toggle-card>

                <rs-section-card
                  icon="mdi:cog"
                  .heading=${localize("room.section.climate_mode", this.hass.language)}
                >
                  <rs-info-icon slot="header-extras">
                    <b>${localize("mode.auto", this.hass.language)}</b> —
                    ${localize("mode.auto_desc", this.hass.language)}<br />
                    <b>${localize("mode.heat_only", this.hass.language)}</b> —
                    ${localize("mode.heat_only_desc", this.hass.language)}<br />
                    <b>${localize("mode.cool_only", this.hass.language)}</b> —
                    ${localize("mode.cool_only_desc", this.hass.language)}
                  </rs-info-icon>
                  <rs-climate-mode-selector
                    .climateMode=${this._climateMode}
                    .language=${this.hass.language}
                    @mode-changed=${this._onModeChanged}
                  ></rs-climate-mode-selector>
                </rs-section-card>

                <rs-section-card
                  icon="mdi:calendar"
                  .heading=${localize("room.section.schedule", this.hass.language)}
                  editable
                  @edit-click=${this._openEdit("schedule")}
                >
                  <rs-schedule-settings
                    .hass=${this.hass}
                    .schedules=${this._schedules}
                    .scheduleSelectorEntity=${this._scheduleSelectorEntity}
                    .activeScheduleIndex=${this.config?.live?.active_schedule_index ?? -1}
                    .comfortHeat=${this._comfortHeat}
                    .comfortCool=${this._comfortCool}
                    .comfortHeatEntity=${this._comfortHeatEntity}
                    .ecoHeat=${this._ecoHeat}
                    .ecoCool=${this._ecoCool}
                    .climateMode=${this._climateMode}
                    .scheduleTempWarnings=${this.config?.live?.schedule_temp_warnings ?? []}
                    .editing=${false}
                    @schedules-changed=${this._onSchedulesChanged}
                    @schedule-selector-changed=${this._onScheduleSelectorChanged}
                    @comfort-heat-changed=${this._onComfortHeatChanged}
                    @comfort-cool-changed=${this._onComfortCoolChanged}
                    @eco-heat-changed=${this._onEcoHeatChanged}
                    @eco-cool-changed=${this._onEcoCoolChanged}
                  ></rs-schedule-settings>
                  ${this.config
                    ? html`
                        <rs-override-section
                          .hass=${this.hass}
                          .config=${this.config}
                          .climateMode=${this._climateMode}
                          .comfortHeat=${this._comfortHeat}
                          .comfortCool=${this._comfortCool}
                          .ecoHeat=${this._ecoHeat}
                          .ecoCool=${this._ecoCool}
                          .language=${this.hass.language}
                        ></rs-override-section>
                      `
                    : nothing}
                </rs-section-card>
              `
            : nothing}
          ${!this._isOutdoor
            ? html`
                <rs-section-card
                  icon="mdi:power-plug"
                  .heading=${localize("room.section.devices", this.hass.language)}
                  editable
                  @edit-click=${this._openEdit("devices")}
                >
                  <rs-device-section
                    .hass=${this.hass}
                    .area=${this.area}
                    .editing=${false}
                    .devices=${this._devices}
                    .selectedTempSensor=${this._selectedTempSensor}
                    .valveProtectionExclude=${this._valveProtectionExclude}
                    .valveProtectionEnabled=${this.valveProtectionEnabled}
                    .coilDryEnabledGlobal=${this.coilDryEnabled}
                    .coilDryMinutesGlobal=${this.coilDryMinutes}
                    .coilDryModeGlobal=${this.coilDryMode}
                    .coilDryFanModeGlobal=${this.coilDryFanMode}
                    @device-changed=${this._onDeviceChanged}
                    @valve-protection-exclude-toggle=${this._onValveProtectionExcludeToggle}
                  ></rs-device-section>
                </rs-section-card>

                <rs-section-card
                  icon="mdi:thermometer"
                  .heading=${localize("room.section.sensors", this.hass.language)}
                  editable
                  @edit-click=${this._openEdit("sensors")}
                >
                  <rs-sensor-section
                    .hass=${this.hass}
                    .area=${this.area}
                    .editing=${false}
                    .temperatureSensor=${this._selectedTempSensor}
                    .humiditySensor=${this._selectedHumiditySensor}
                    .occupancySensors=${this._selectedOccupancySensors}
                    .windowSensors=${this._selectedWindowSensors}
                    .windowOpenDelay=${this._windowOpenDelay}
                    .windowCloseDelay=${this._windowCloseDelay}
                    .heatingSystemType=${resolveHeatingSystemType(this._devices)}
                    .language=${this.hass.language}
                    @sensor-changed=${this._onSensorChanged}
                  ></rs-sensor-section>
                </rs-section-card>

                ${this.presenceEnabled &&
                (this.presencePersons.length > 0 || this._selectedPresencePersons.length > 0)
                  ? html`<rs-section-card
                      icon="mdi:home-account"
                      .heading=${localize("room.section.presence", this.hass.language)}
                      editable
                      @edit-click=${this._openEdit("presence")}
                    >
                      <rs-info-icon
                        slot="header-extras"
                        .text=${localize("presence.ignore_hint", this.hass.language)}
                      ></rs-info-icon>
                      <rs-presence-section
                        .hass=${this.hass}
                        .presenceEnabled=${this.presenceEnabled}
                        .presencePersons=${this.presencePersons}
                        .selectedPresencePersons=${this._selectedPresencePersons}
                        .ignorePresence=${this._ignorePresence}
                        .editing=${false}
                        .language=${this.hass.language}
                        @presence-persons-changed=${this._onPresencePersonsChanged}
                        @ignore-presence-changed=${this._onIgnorePresenceChanged}
                      ></rs-presence-section>
                    </rs-section-card>`
                  : nothing}
              `
            : nothing}
          ${!this._isOutdoor
            ? html`<rs-section-card
                icon="mdi:blinds-horizontal"
                .heading=${localize("room.section.covers", this.hass.language)}
                .badge=${localize("badge.beta", this.hass.language)}
                .badgeHint=${localize("badge.beta_hint", this.hass.language)}
                editable
                @edit-click=${this._openEdit("covers")}
              >
                <rs-covers-section
                  .hass=${this.hass}
                  .area=${this.area}
                  .editing=${false}
                  .selectedCovers=${this._selectedCovers}
                  .autoEnabled=${this._coversAutoEnabled}
                  .deployThreshold=${this._coversDeployThreshold}
                  .minPosition=${this._coversMinPosition}
                  .overrideMinutes=${this._coversOverrideMinutes}
                  .coverSchedules=${this._coverSchedules}
                  .coverScheduleSelectorEntity=${this._coverScheduleSelectorEntity}
                  .activeCoverScheduleIndex=${this.config?.live?.active_cover_schedule_index ?? -1}
                  .nightClose=${this._coversNightClose}
                  .nightPosition=${this._coversNightPosition}
                  .snapDeploy=${this._coversSnapDeploy}
                  .forcedReason=${this.config?.live?.cover_forced_reason ?? ""}
                  .autoPaused=${this._optimisticCoverResume
                    ? false
                    : (this.config?.live?.cover_auto_paused ?? false)}
                  .overrideUntil=${this.config?.live?.cover_override_until ?? null}
                  .coverOrientations=${this._coverOrientations}
                  .nightCloseElevation=${this._coversNightCloseElevation}
                  .nightCloseOffsetMinutes=${this._coversNightCloseOffsetMinutes}
                  .outdoorMinTemp=${this._coversOutdoorMinTemp}
                  .coverMinPositions=${this._coverMinPositions}
                  @covers-toggle=${this._onCoversToggle}
                  @setting-changed=${this._onCoverSettingChanged}
                  @cover-resume-auto=${this._onCoverResumeAuto}
                ></rs-covers-section>
              </rs-section-card>`
            : nothing}
          ${!this._isOutdoor &&
          this._selectedTempSensor &&
          this._devices.some((d) => d.type === "trv") &&
          this._devices.some((d) => d.type === "ac")
            ? html`<rs-section-card
                icon="mdi:swap-horizontal"
                .heading=${localize("room.section.heat_source", this.hass.language)}
                editable
                @edit-click=${this._openEdit("heatSource")}
              >
                <rs-heat-source-section
                  .hass=${this.hass}
                  .editing=${false}
                  .enabled=${this._heatSourceOrchestration}
                  .primaryDelta=${this._heatSourcePrimaryDelta}
                  .outdoorThreshold=${this._heatSourceOutdoorThreshold}
                  .acMinOutdoor=${this._heatSourceAcMinOutdoor}
                  @setting-changed=${this._onHeatSourceSettingChanged}
                ></rs-heat-source-section>
              </rs-section-card>`
            : nothing}

          <rs-toggle-card
            icon="mdi:tree"
            .label=${localize("room.outdoor_toggle", this.hass.language)}
            .hint=${localize("room.outdoor_hint", this.hass.language)}
            .checked=${this._isOutdoor}
            @toggle-changed=${this._onOutdoorToggle}
          ></rs-toggle-card>
        </div>
        ${this._error ? html`<div class="error">${this._error}</div>` : nothing}
        ${this._renderEditDialog()}
      </div>
    `;
  }

  private _renderEditDialog() {
    if (this._editing === null) return nothing;
    const lang = this.hass.language;

    switch (this._editing) {
      case "schedule":
        return html`<rs-edit-dialog
          open
          icon="mdi:calendar"
          .heading=${localize("room.section.schedule", lang)}
          hasInfo
          @rs-dialog-closed=${this._closeEdit}
        >
          <div slot="info">
            <p><strong>${localize("schedule.help_temps_title", lang)}</strong></p>
            <p>${localize("schedule.help_temps", lang)}</p>
            <ol style="margin: 4px 0 0 0; padding-left: 20px; line-height: 1.8">
              <li>${unsafeHTML(localize("schedule.help_temps_1", lang))}</li>
              <li>${unsafeHTML(localize("schedule.help_temps_2", lang))}</li>
              <li>${unsafeHTML(localize("schedule.help_temps_3", lang))}</li>
              <li>${unsafeHTML(localize("schedule.help_temps_4", lang))}</li>
            </ol>
            <p style="margin-top: 12px">
              <strong>${localize("schedule.help_block_title", lang)}</strong>
            </p>
            <p>${unsafeHTML(localize("schedule.help_block", lang))}</p>
            <div class="yaml-block">
              ${unsafeHTML(
                '<span class="yaml-key">schedule</span>:\n' +
                  '  <span class="yaml-key">living_room_heating</span>:\n' +
                  '    <span class="yaml-key">name</span>: <span class="yaml-value">Living Room Heating</span>\n' +
                  '    <span class="yaml-key">monday</span>:\n' +
                  '      - <span class="yaml-key">from</span>: <span class="yaml-value">"06:00:00"</span>\n' +
                  '        <span class="yaml-key">to</span>: <span class="yaml-value">"08:00:00"</span>\n' +
                  '        <span class="yaml-key">data</span>:\n' +
                  '          <span class="yaml-key">temperature</span>: <span class="yaml-value">23</span>\n' +
                  '      - <span class="yaml-key">from</span>: <span class="yaml-value">"17:00:00"</span>\n' +
                  '        <span class="yaml-key">to</span>: <span class="yaml-value">"22:00:00"</span>\n' +
                  '        <span class="yaml-key">data</span>:\n' +
                  '          <span class="yaml-key">temperature</span>: <span class="yaml-value">21.5</span>',
              )}
            </div>
            <p style="margin-top: 8px">${unsafeHTML(localize("schedule.help_block_note", lang))}</p>
            <p style="margin-top: 12px">
              <strong>${localize("schedule.help_split_title", lang)}</strong>
            </p>
            <p>${unsafeHTML(localize("schedule.help_split", lang))}</p>
            <div class="yaml-block">
              ${unsafeHTML(
                '- <span class="yaml-key">from</span>: <span class="yaml-value">"06:00:00"</span>\n' +
                  '  <span class="yaml-key">to</span>: <span class="yaml-value">"08:00:00"</span>\n' +
                  '  <span class="yaml-key">data</span>:\n' +
                  '    <span class="yaml-key">heat_temperature</span>: <span class="yaml-value">21</span>\n' +
                  '    <span class="yaml-key">cool_temperature</span>: <span class="yaml-value">24</span>',
              )}
            </div>
            <p style="margin-top: 8px">${unsafeHTML(localize("schedule.help_split_note", lang))}</p>
            <p style="margin-top: 12px">
              <strong>${localize("schedule.help_multi_title", lang)}</strong>
            </p>
            <p>${unsafeHTML(localize("schedule.help_multi", lang))}</p>
          </div>
          <rs-schedule-settings
            .hass=${this.hass}
            .schedules=${this._schedules}
            .scheduleSelectorEntity=${this._scheduleSelectorEntity}
            .activeScheduleIndex=${this.config?.live?.active_schedule_index ?? -1}
            .comfortHeat=${this._comfortHeat}
            .comfortCool=${this._comfortCool}
            .comfortHeatEntity=${this._comfortHeatEntity}
            .ecoHeat=${this._ecoHeat}
            .ecoCool=${this._ecoCool}
            .climateMode=${this._climateMode}
            .scheduleTempWarnings=${this.config?.live?.schedule_temp_warnings ?? []}
            .editing=${true}
            @schedules-changed=${this._onSchedulesChanged}
            @schedule-selector-changed=${this._onScheduleSelectorChanged}
            @comfort-source-changed=${this._onComfortSourceChanged}
            @comfort-heat-changed=${this._onComfortHeatChanged}
            @comfort-cool-changed=${this._onComfortCoolChanged}
            @eco-heat-changed=${this._onEcoHeatChanged}
            @eco-cool-changed=${this._onEcoCoolChanged}
          ></rs-schedule-settings>
        </rs-edit-dialog>`;
      case "devices":
        return html`<rs-edit-dialog
          open
          icon="mdi:power-plug"
          .heading=${localize("room.section.devices", lang)}
          hasInfo
          @rs-dialog-closed=${this._closeEdit}
        >
          <div slot="info">
            <b>${localize("devices.info.types_title", lang)}</b><br />
            ${localize("devices.info.types_body", lang)}
            <br /><br />
            <b>${localize("devices.info.control_title", lang)}</b><br />
            ${localize("devices.info.control_body", lang)}
            <br /><br />
            <b>${localize("devices.info.modes_title", lang)}</b><br />
            ${localize("devices.info.modes_body", lang)}
            <br /><br />
            <b>${localize("devices.info.heat_source_title", lang)}</b><br />
            ${localize("devices.info.heat_source_body", lang)}
            <br />
            <a class="helper-link" href=${CONTROL_DOCS_URL} target="_blank" rel="noreferrer">
              ${localize("common.learn_more", lang)}
            </a>
          </div>
          <rs-device-section
            .hass=${this.hass}
            .area=${this.area}
            .editing=${true}
            .devices=${this._devices}
            .selectedTempSensor=${this._selectedTempSensor}
            .valveProtectionExclude=${this._valveProtectionExclude}
            .valveProtectionEnabled=${this.valveProtectionEnabled}
            .coilDryEnabledGlobal=${this.coilDryEnabled}
            .coilDryMinutesGlobal=${this.coilDryMinutes}
            .coilDryModeGlobal=${this.coilDryMode}
            .coilDryFanModeGlobal=${this.coilDryFanMode}
            @device-changed=${this._onDeviceChanged}
            @valve-protection-exclude-toggle=${this._onValveProtectionExcludeToggle}
          ></rs-device-section>
        </rs-edit-dialog>`;
      case "sensors":
        return html`<rs-edit-dialog
          open
          icon="mdi:thermometer"
          .heading=${localize("room.section.sensors", lang)}
          @rs-dialog-closed=${this._closeEdit}
        >
          <rs-sensor-section
            .hass=${this.hass}
            .area=${this.area}
            .editing=${true}
            .temperatureSensor=${this._selectedTempSensor}
            .humiditySensor=${this._selectedHumiditySensor}
            .occupancySensors=${this._selectedOccupancySensors}
            .windowSensors=${this._selectedWindowSensors}
            .windowOpenDelay=${this._windowOpenDelay}
            .windowCloseDelay=${this._windowCloseDelay}
            .heatingSystemType=${resolveHeatingSystemType(this._devices)}
            .language=${this.hass.language}
            @sensor-changed=${this._onSensorChanged}
          ></rs-sensor-section>
        </rs-edit-dialog>`;
      case "presence":
        return html`<rs-edit-dialog
          open
          icon="mdi:home-account"
          .heading=${localize("room.section.presence", lang)}
          hasInfo
          @rs-dialog-closed=${this._closeEdit}
        >
          <div slot="info">
            <b>${localize("presence.room_help_header", lang)}</b><br />
            ${localize("presence.room_help_body", lang)}
            <br /><br />
            <b>${localize("presence.help_ignore_title", lang)}</b><br />
            ${localize("presence.help_ignore_body", lang)}
          </div>
          <rs-presence-section
            .hass=${this.hass}
            .presenceEnabled=${this.presenceEnabled}
            .presencePersons=${this.presencePersons}
            .selectedPresencePersons=${this._selectedPresencePersons}
            .ignorePresence=${this._ignorePresence}
            .editing=${true}
            .language=${this.hass.language}
            @presence-persons-changed=${this._onPresencePersonsChanged}
            @ignore-presence-changed=${this._onIgnorePresenceChanged}
          ></rs-presence-section>
        </rs-edit-dialog>`;
      case "covers":
        return html`<rs-edit-dialog
          open
          icon="mdi:blinds-horizontal"
          .heading=${localize("room.section.covers", lang)}
          hasInfo
          @rs-dialog-closed=${this._closeEdit}
        >
          <div slot="info">
            <b>${localize("covers.info.selection_title", lang)}</b><br />
            ${localize("covers.info.selection_body", lang)}
            <br /><br />
            <b>${localize("covers.info.schedule_title", lang)}</b><br />
            ${localize("covers.info.schedule_body", lang)}
            <div class="yaml-block">
              ${unsafeHTML(
                '<span class="yaml-key">schedule</span>:\n' +
                  '  <span class="yaml-key">cover_evening</span>:\n' +
                  '    <span class="yaml-key">name</span>: <span class="yaml-value">Cover Evening</span>\n' +
                  '    <span class="yaml-key">monday</span>:\n' +
                  '      - <span class="yaml-key">from</span>: <span class="yaml-value">"20:00:00"</span>\n' +
                  '        <span class="yaml-key">to</span>: <span class="yaml-value">"06:00:00"</span>\n' +
                  '        <span class="yaml-key">data</span>:\n' +
                  '          <span class="yaml-key">position</span>: <span class="yaml-value">10</span>',
              )}
            </div>
            <b>${localize("covers.info.solar_title", lang)}</b><br />
            ${localize("covers.info.solar_body", lang)}
            <br /><br />
            <b>${localize("covers.info.night_title", lang)}</b><br />
            ${localize("covers.info.night_body", lang)}
            <br /><br />
            <b>${localize("covers.info.override_title", lang)}</b><br />
            ${localize("covers.info.override_body", lang)}
            <br /><br />
            <b>${localize("covers.info.priority_title", lang)}</b><br />
            ${localize("covers.info.priority_body", lang)}
            <br /><br />
            <b>${localize("covers.info.entities_title", lang)}</b><br />
            ${localize("covers.info.entities_body", lang)}
          </div>
          <rs-covers-section
            .hass=${this.hass}
            .area=${this.area}
            .editing=${true}
            .selectedCovers=${this._selectedCovers}
            .autoEnabled=${this._coversAutoEnabled}
            .deployThreshold=${this._coversDeployThreshold}
            .minPosition=${this._coversMinPosition}
            .overrideMinutes=${this._coversOverrideMinutes}
            .coverSchedules=${this._coverSchedules}
            .coverScheduleSelectorEntity=${this._coverScheduleSelectorEntity}
            .activeCoverScheduleIndex=${this.config?.live?.active_cover_schedule_index ?? -1}
            .nightClose=${this._coversNightClose}
            .nightPosition=${this._coversNightPosition}
            .snapDeploy=${this._coversSnapDeploy}
            .forcedReason=${this.config?.live?.cover_forced_reason ?? ""}
            .autoPaused=${this._optimisticCoverResume
              ? false
              : (this.config?.live?.cover_auto_paused ?? false)}
            .overrideUntil=${this.config?.live?.cover_override_until ?? null}
            .coverOrientations=${this._coverOrientations}
            .nightCloseElevation=${this._coversNightCloseElevation}
            .nightCloseOffsetMinutes=${this._coversNightCloseOffsetMinutes}
            .outdoorMinTemp=${this._coversOutdoorMinTemp}
            .coverMinPositions=${this._coverMinPositions}
            @covers-toggle=${this._onCoversToggle}
            @setting-changed=${this._onCoverSettingChanged}
            @cover-resume-auto=${this._onCoverResumeAuto}
          ></rs-covers-section>
        </rs-edit-dialog>`;
      case "heatSource":
        return html`<rs-edit-dialog
          open
          icon="mdi:swap-horizontal"
          .heading=${localize("room.section.heat_source", lang)}
          @rs-dialog-closed=${this._closeEdit}
        >
          <rs-heat-source-section
            .hass=${this.hass}
            .editing=${true}
            .enabled=${this._heatSourceOrchestration}
            .primaryDelta=${this._heatSourcePrimaryDelta}
            .outdoorThreshold=${this._heatSourceOutdoorThreshold}
            .acMinOutdoor=${this._heatSourceAcMinOutdoor}
            @setting-changed=${this._onHeatSourceSettingChanged}
          ></rs-heat-source-section>
        </rs-edit-dialog>`;
    }
  }

  // ---- Child event handlers ----

  private _onModeChanged(e: CustomEvent<{ mode: ClimateMode }>) {
    this._climateMode = e.detail.mode;
    this._autoSave();
  }

  private _onSchedulesChanged(e: CustomEvent<{ value: ScheduleEntry[] }>) {
    this._schedules = e.detail.value;
    this._autoSave();
  }

  private _onScheduleSelectorChanged(e: CustomEvent<{ value: string }>) {
    this._scheduleSelectorEntity = e.detail.value;
    this._autoSave();
  }

  private _onComfortHeatChanged(e: CustomEvent<{ value: number }>) {
    this._comfortHeat = e.detail.value;
    if (this._comfortCool < this._comfortHeat) this._comfortCool = this._comfortHeat;
    this._autoSave();
  }

  private _onComfortSourceChanged(e: CustomEvent<{ value: string }>) {
    this._comfortHeatEntity = e.detail.value;
    this._autoSave();
  }

  private _onComfortCoolChanged(e: CustomEvent<{ value: number }>) {
    this._comfortCool = e.detail.value;
    if (this._comfortHeat > this._comfortCool) this._comfortHeat = this._comfortCool;
    this._autoSave();
  }

  private _onEcoHeatChanged(e: CustomEvent<{ value: number }>) {
    this._ecoHeat = e.detail.value;
    if (this._ecoCool < this._ecoHeat) this._ecoCool = this._ecoHeat;
    this._autoSave();
  }

  private _onEcoCoolChanged(e: CustomEvent<{ value: number }>) {
    this._ecoCool = e.detail.value;
    if (this._ecoHeat > this._ecoCool) this._ecoHeat = this._ecoCool;
    this._autoSave();
  }

  private _onDeviceChanged(e: CustomEvent<{ devices: DeviceConfig[] }>) {
    const oldDeviceIds = new Set(this._devices.map((d) => d.entity_id));
    this._devices = e.detail.devices;
    const newDeviceIds = new Set(this._devices.map((d) => d.entity_id));

    // Clean up valve protection exclude list for removed devices
    for (const eid of oldDeviceIds) {
      if (!newDeviceIds.has(eid) && this._valveProtectionExclude.has(eid)) {
        const nextExclude = new Set(this._valveProtectionExclude);
        nextExclude.delete(eid);
        this._valveProtectionExclude = nextExclude;
      }
    }

    // Moving to non-TRV: remove from valve protection exclude list
    for (const d of this._devices) {
      if (d.type !== "trv" && this._valveProtectionExclude.has(d.entity_id)) {
        const nextExclude = new Set(this._valveProtectionExclude);
        nextExclude.delete(d.entity_id);
        this._valveProtectionExclude = nextExclude;
      }
    }

    this._autoSave();
  }

  private _onSensorChanged(e: CustomEvent<{ key: string; value: string | string[] | number }>) {
    const { key, value } = e.detail;
    if (key === "temperature_sensor") {
      this._selectedTempSensor = value as string;
    } else if (key === "humidity_sensor") {
      this._selectedHumiditySensor = value as string;
    } else if (key === "occupancy_sensors") {
      this._selectedOccupancySensors = new Set(value as string[]);
    } else if (key === "window_sensors") {
      this._selectedWindowSensors = new Set(value as string[]);
    } else if (key === "window_open_delay") {
      this._windowOpenDelay = value as number;
    } else if (key === "window_close_delay") {
      this._windowCloseDelay = value as number;
    }
    this._autoSave();
  }

  private _onValveProtectionExcludeToggle(e: CustomEvent<{ entityId: string; excluded: boolean }>) {
    const { entityId, excluded } = e.detail;
    const next = new Set(this._valveProtectionExclude);
    if (excluded) {
      next.add(entityId);
    } else {
      next.delete(entityId);
    }
    this._valveProtectionExclude = next;
    this._autoSave();
  }

  private _onPresencePersonsChanged(e: CustomEvent<string[]>) {
    this._selectedPresencePersons = e.detail;
    this._autoSave();
  }

  private _onIgnorePresenceChanged(e: CustomEvent<boolean>) {
    this._ignorePresence = e.detail;
    this._autoSave();
  }

  // ---- Cover event handlers ----

  private _onCoversToggle(e: CustomEvent<{ entityId: string; checked: boolean }>) {
    const { entityId, checked } = e.detail;
    const next = new Set(this._selectedCovers);
    if (checked) {
      next.add(entityId);
    } else {
      next.delete(entityId);
      if (entityId in this._coverOrientations) {
        const nextOrientations = { ...this._coverOrientations };
        delete nextOrientations[entityId];
        this._coverOrientations = nextOrientations;
      }
      if (entityId in this._coverMinPositions) {
        const nextMinPositions = { ...this._coverMinPositions };
        delete nextMinPositions[entityId];
        this._coverMinPositions = nextMinPositions;
      }
    }
    this._selectedCovers = next;
    this._autoSave();
  }

  private _onCoverSettingChanged(e: CustomEvent<{ key: string; value: unknown }>) {
    const { key, value } = e.detail;
    e.stopPropagation();
    if (key === "covers_auto_enabled") this._coversAutoEnabled = value as boolean;
    else if (key === "covers_deploy_threshold") this._coversDeployThreshold = value as number;
    else if (key === "covers_min_position") this._coversMinPosition = value as number;
    else if (key === "covers_override_minutes") this._coversOverrideMinutes = value as number;
    else if (key === "cover_schedules") this._coverSchedules = value as CoverScheduleEntry[];
    else if (key === "cover_schedule_selector_entity")
      this._coverScheduleSelectorEntity = value as string;
    else if (key === "covers_night_close") this._coversNightClose = value as boolean;
    else if (key === "covers_night_position") this._coversNightPosition = value as number;
    else if (key === "covers_snap_deploy") this._coversSnapDeploy = value as boolean;
    else if (key === "cover_orientations")
      this._coverOrientations = value as Record<string, number>;
    else if (key === "covers_night_close_elevation")
      this._coversNightCloseElevation = value as number;
    else if (key === "covers_night_close_offset_minutes")
      this._coversNightCloseOffsetMinutes = value as number;
    else if (key === "covers_outdoor_min_temp") this._coversOutdoorMinTemp = value as number | null;
    else if (key === "cover_min_positions")
      this._coverMinPositions = value as Record<string, number>;
    this._autoSave();
  }

  private async _onCoverResumeAuto() {
    this._optimisticCoverResume = true;
    try {
      await this.hass.callWS({
        type: "roommind/covers/clear_override",
        area_id: this.area.area_id,
      });
    } catch {
      this._optimisticCoverResume = false;
    }
  }

  // ---- Heat source orchestration ----

  private _onHeatSourceSettingChanged(e: CustomEvent<{ key: string; value: unknown }>) {
    const { key, value } = e.detail;
    e.stopPropagation();
    if (key === "heat_source_orchestration") this._heatSourceOrchestration = value as boolean;
    else if (key === "heat_source_primary_delta") this._heatSourcePrimaryDelta = value as number;
    else if (key === "heat_source_outdoor_threshold")
      this._heatSourceOutdoorThreshold = value as number;
    else if (key === "heat_source_ac_min_outdoor") this._heatSourceAcMinOutdoor = value as number;
    this._autoSave();
  }

  // ---- Outdoor toggle ----

  private _onClimateControlToggle(e: CustomEvent) {
    this._climateControlEnabled = e.detail;
    this._autoSave();
  }

  private _onOutdoorToggle(e: CustomEvent<boolean>) {
    this._isOutdoor = e.detail;
    this._autoSave();
  }

  // ---- Auto-save ----

  private _onDisplayNameChanged(e: CustomEvent<{ value: string }>) {
    this._displayName = e.detail.value;
    this._autoSave();
  }

  private _autoSave() {
    this._dirty = true;
    if (this._saveDebounce) clearTimeout(this._saveDebounce);
    this._saveDebounce = setTimeout(() => this._doSave(), 500);
  }

  private async _doSave() {
    fireSaveStatus(this, "saving");
    this._error = "";

    try {
      await this.hass.callWS({
        type: "roommind/rooms/save",
        area_id: this.area.area_id,
        devices: this._devices,
        temperature_sensor: this._selectedTempSensor,
        humidity_sensor: this._selectedHumiditySensor,
        occupancy_sensors: [...this._selectedOccupancySensors],
        window_sensors: [...this._selectedWindowSensors],
        window_open_delay: this._windowOpenDelay,
        window_close_delay: this._windowCloseDelay,
        climate_mode: this._climateMode,
        schedules: this._schedules,
        schedule_selector_entity: this._scheduleSelectorEntity,
        comfort_heat: this._comfortHeat,
        comfort_cool: this._comfortCool,
        comfort_heat_entity: this._comfortHeatEntity,
        eco_heat: this._ecoHeat,
        eco_cool: this._ecoCool,
        presence_persons: this._selectedPresencePersons.filter((p) => p),
        display_name: this._displayName,
        covers: [...this._selectedCovers],
        climate_control_enabled: this._climateControlEnabled,
        covers_auto_enabled: this._coversAutoEnabled,
        covers_deploy_threshold: this._coversDeployThreshold,
        covers_min_position: this._coversMinPosition,
        covers_override_minutes: this._coversOverrideMinutes,
        cover_schedules: this._coverSchedules,
        cover_schedule_selector_entity: this._coverScheduleSelectorEntity,
        covers_night_close: this._coversNightClose,
        covers_night_position: this._coversNightPosition,
        covers_snap_deploy: this._coversSnapDeploy,
        cover_orientations: this._coverOrientations,
        covers_night_close_elevation: this._coversNightCloseElevation,
        covers_night_close_offset_minutes: this._coversNightCloseOffsetMinutes,
        covers_outdoor_min_temp: this._coversOutdoorMinTemp,
        cover_min_positions: this._coverMinPositions,
        ignore_presence: this._ignorePresence,
        is_outdoor: this._isOutdoor,
        valve_protection_exclude: [...this._valveProtectionExclude],
        heat_source_orchestration: this._heatSourceOrchestration,
        heat_source_primary_delta: this._heatSourcePrimaryDelta,
        heat_source_outdoor_threshold: this._heatSourceOutdoorThreshold,
        heat_source_ac_min_outdoor: this._heatSourceAcMinOutdoor,
      });

      this._dirty = false;
      fireSaveStatus(this, "saved");

      this.dispatchEvent(
        new CustomEvent("room-updated", {
          bubbles: true,
          composed: true,
        }),
      );
    } catch (err: unknown) {
      const message =
        err instanceof Error
          ? err.message
          : localize("room.error_save_fallback", this.hass.language);
      this._error = message;
      fireSaveStatus(this, "error");
    }
  }
}

declare global {
  interface HTMLElementTagNameMap {
    "rs-room-detail": RsRoomDetail;
  }
}
