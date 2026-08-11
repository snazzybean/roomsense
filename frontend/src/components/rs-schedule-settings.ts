import { html, css, nothing } from "lit";
import { customElement, property } from "lit/decorators.js";
import type { ScheduleEntry, ClimateMode, ScheduleTempWarning } from "../types";
import { localize } from "../utils/localize";
import {
  formatTemp,
  tempUnit,
  toDisplay,
  toCelsius,
  isPlausibleTargetTemp,
  MIN_TARGET_TEMP_C,
  MAX_TARGET_TEMP_C,
  tempStep,
  tempRange,
} from "../utils/temperature";
import { RsScheduleBase } from "./shared/rs-schedule-base";
import { inputStyles } from "../styles/input-styles";

/** Mirrors _WEEKDAYS in the backend's schedule_utils.py. */
const WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"];

@customElement("rs-schedule-settings")
export class RsScheduleSettings extends RsScheduleBase {
  @property({ attribute: false }) public schedules: ScheduleEntry[] = [];
  /** Alias: parent passes .scheduleSelectorEntity, base uses selectorEntity. */
  @property({ type: String }) public set scheduleSelectorEntity(v: string) {
    this.selectorEntity = v;
  }
  public get scheduleSelectorEntity(): string {
    return this.selectorEntity;
  }
  /** Alias: parent passes .activeScheduleIndex, base uses activeIndex. */
  @property({ type: Number }) public set activeScheduleIndex(v: number) {
    this.activeIndex = v;
  }
  public get activeScheduleIndex(): number {
    return this.activeIndex;
  }
  @property({ type: Number }) public comfortHeat = 21.0;
  @property({ type: Number }) public comfortCool = 24.0;
  @property({ type: Number }) public ecoHeat = 17.0;
  @property({ type: Number }) public ecoCool = 27.0;
  @property({ type: String }) public climateMode: ClimateMode = "auto";
  @property({ attribute: false }) public scheduleTempWarnings: ScheduleTempWarning[] = [];
  /** Optional climate entity (e.g. a wall display) whose setpoint is the comfort heat temp. */
  @property({ type: String }) public comfortHeatEntity = "";

  static styles = [
    RsScheduleBase.sharedStyles,
    inputStyles,
    css`
      .fallback-hint {
        font-size: 11px;
        color: var(--secondary-text-color);
        margin-top: 4px;
        font-style: italic;
      }

      .temp-inputs {
        display: grid;
        grid-template-columns: 1fr 1fr;
        gap: 16px;
        margin-top: 16px;
      }

      .temp-input-group {
        display: flex;
        flex-direction: column;
        gap: 6px;
      }

      ha-textfield {
        flex: 1;
      }

      .view-temps {
        display: flex;
        gap: 16px;
        font-size: 13px;
        color: var(--secondary-text-color);
        margin-top: 12px;
        padding-top: 12px;
        border-top: 1px solid var(--divider-color, #eee);
      }

      .view-temps span {
        font-weight: 500;
        color: var(--primary-text-color);
      }

      .view-selector-info {
        font-size: 12px;
        color: var(--secondary-text-color);
        margin-top: 8px;
      }

      .temp-grid-auto {
        display: grid;
        grid-template-columns: auto 1fr 1fr;
        gap: 8px 12px;
        align-items: center;
        margin-top: 16px;
      }
      .temp-grid-header {
        font-size: 12px;
        font-weight: 600;
        color: var(--secondary-text-color);
        text-transform: uppercase;
        letter-spacing: 0.3px;
        text-align: center;
      }
      .temp-grid-row-label {
        display: flex;
        align-items: center;
        gap: 4px;
        font-size: 13px;
        font-weight: 500;
        color: var(--secondary-text-color);
        white-space: nowrap;
      }

      @media (max-width: 600px) {
        .temp-grid-auto {
          grid-template-columns: 1fr 1fr;
        }
        .temp-grid-row-label {
          grid-column: 1 / -1;
          margin-top: 8px;
        }
        .temp-grid-header {
          display: none;
        }
      }
    `,
  ];

  render() {
    if (!this.editing) {
      return this._renderViewMode();
    }
    return this._renderEditMode();
  }

  // ─── View mode ─────────────────────────────────────────────────

  private _renderViewMode() {
    const l = this.hass.language;
    const hasMultiple = this.schedules.length >= 2;

    return html`
      ${this.schedules.length > 0
        ? html`
            <div class="schedule-list">
              ${this.schedules.map((schedule, index) => {
                const state = this._getScheduleState(index, this.schedules.length);
                return html`
                  <div class="schedule-row ${state}">
                    ${hasMultiple
                      ? html`<span class="schedule-number">${index + 1}</span>`
                      : nothing}
                    <span class="schedule-status-dot"></span>
                    <span
                      class="schedule-name schedule-link"
                      @click=${() => this._openEntityInfo(schedule.entity_id)}
                      >${this._getFriendlyName(schedule.entity_id)}</span
                    >
                    <span class="schedule-status">${this._getStatusText(index, state)}</span>
                  </div>
                  ${this._renderRejectedTempWarning(state)}
                `;
              })}
            </div>
          `
        : html`<div class="no-schedules">${localize("schedule.no_schedules", l)}</div>`}
      ${this.climateMode === "auto"
        ? html`
            <div class="view-temps">
              ${localize("schedule.view_heat", l, {
                comfort: formatTemp(this.comfortHeat, this.hass),
                eco: formatTemp(this.ecoHeat, this.hass),
                unit: tempUnit(this.hass),
              })}
               · 
              ${localize("schedule.view_cool", l, {
                comfort: formatTemp(this.comfortCool, this.hass),
                eco: formatTemp(this.ecoCool, this.hass),
                unit: tempUnit(this.hass),
              })}
            </div>
          `
        : html`
            <div class="view-temps">
              ${localize("schedule.view_comfort", l, {
                temp: formatTemp(
                  this.climateMode === "cool_only" ? this.comfortCool : this.comfortHeat,
                  this.hass,
                ),
                unit: tempUnit(this.hass),
              })}
               · 
              ${localize("schedule.view_eco", l, {
                temp: formatTemp(
                  this.climateMode === "cool_only" ? this.ecoCool : this.ecoHeat,
                  this.hass,
                ),
                unit: tempUnit(this.hass),
              })}
            </div>
          `}
      ${this.scheduleSelectorEntity
        ? html`<div class="view-selector-info">
            ${localize("schedule.view_selector_prefix", l)}
            <span
              class="schedule-link"
              @click=${() => this._openEntityInfo(this.scheduleSelectorEntity!)}
              >${this._getFriendlyName(this.scheduleSelectorEntity)}</span
            >
          </div>`
        : nothing}
      ${this.comfortHeatEntity
        ? html`<div class="view-selector-info">
            ${localize("schedule.comfort_source_prefix", l)}
            <span class="schedule-link" @click=${() => this._openEntityInfo(this.comfortHeatEntity)}
              >${this._getFriendlyName(this.comfortHeatEntity)}</span
            >
          </div>`
        : nothing}
    `;
  }

  // ─── Edit mode ─────────────────────────────────────────────────

  private _renderEditMode() {
    const l = this.hass.language;
    const count = this.schedules.length;
    const usedIds = new Set(this.schedules.map((s) => s.entity_id));

    return html`
      ${this._renderScheduleList()}
      ${this._renderAddRow(
        localize("schedule.select_schedule", l),
        this._getAvailableEntities(usedIds),
        (eid) => this._addSchedule(eid),
        localize("schedule.create_helper_hint", l),
      )}
      ${this._renderSelectorSection(
        count,
        localize("schedule.selector_label", l),
        this.scheduleSelectorEntity ? this._getSelectorValueText(l) : "",
        localize("schedule.selector_warning", l),
        (value) => this._onSelectorEntityChange(value),
      )}
      ${this._renderTemperatureInputs(l)} ${this._renderComfortSourceSection(l)}
    `;
  }

  // ─── Comfort source entity ─────────────────────────────────────

  /** Setpoint of the comfort source entity, in display units, or null. */
  private _comfortSourceSetpoint(): number | null {
    const state = this.comfortHeatEntity ? this.hass?.states?.[this.comfortHeatEntity] : null;
    if (!state || state.state === "unavailable" || state.state === "unknown") return null;
    const attrs = state.attributes ?? {};
    const raw = (attrs.temperature ?? attrs.target_temp_low) as number | undefined;
    return typeof raw === "number" ? raw : null;
  }

  private _renderComfortSourceSection(l: string) {
    const setpoint = this._comfortSourceSetpoint();

    return html`
      <div class="selector-section">
        <label class="form-label">${localize("schedule.comfort_source_label", l)}</label>
        <ha-entity-picker
          .hass=${this.hass}
          .value=${this.comfortHeatEntity}
          .includeDomains=${["climate"]}
          allow-custom-entity
          @value-changed=${this._onComfortSourceChanged}
        ></ha-entity-picker>
        ${this.comfortHeatEntity && setpoint !== null
          ? html`<div class="selector-value">
              ${localize("schedule.comfort_source_value", l, {
                temp: String(setpoint),
                unit: tempUnit(this.hass),
              })}
            </div>`
          : nothing}
        <div class="section-hint" style="margin-top:4px">
          ${localize("schedule.comfort_source_hint", l)}
        </div>
      </div>
    `;
  }

  private _onComfortSourceChanged(e: CustomEvent) {
    e.stopPropagation();
    const value = e.detail?.value ?? "";
    if (value === this.comfortHeatEntity) return;
    this.comfortHeatEntity = value;
    this.dispatchEvent(
      new CustomEvent("comfort-source-changed", {
        detail: { value },
        bubbles: true,
        composed: true,
      }),
    );
  }

  // ─── Schedule list ─────────────────────────────────────────────

  private _renderScheduleList() {
    const l = this.hass.language;
    const count = this.schedules.length;

    if (count === 0) {
      return html`<div class="no-schedules">${localize("schedule.no_schedules", l)}</div>`;
    }

    return html`
      <div class="schedule-list">
        ${this.schedules.map((schedule, index) => {
          const state = this._getScheduleState(index, count);
          return html`
            <div class="schedule-row ${state}">
              ${count >= 2 ? html`<span class="schedule-number">${index + 1}</span>` : nothing}
              <span class="schedule-status-dot"></span>
              <span class="schedule-name">${this._getFriendlyName(schedule.entity_id)}</span>
              <span class="schedule-status">${this._getStatusText(index, state)}</span>
              ${this._renderScheduleControls(
                index,
                count,
                (i, dir) => this._moveSchedule(i, dir),
                (i) => this._removeSchedule(i),
              )}
            </div>
            ${this._renderRejectedTempWarning(state)}
          `;
        })}
      </div>
    `;
  }

  /**
   * Warn about block temperatures the backend discarded (#395).
   *
   * Sourced from the backend's whole-week scan, so a typo in the 03:00 block is
   * visible at any time of day — not just while that block is running. The scan
   * covers the active schedule only, hence the warning hangs off that row.
   */
  private _renderRejectedTempWarning(state: "active" | "inactive" | "unreachable") {
    if (state !== "active") return nothing;
    const warnings = this.scheduleTempWarnings ?? [];
    if (warnings.length === 0) return nothing;

    const range = tempRange(MIN_TARGET_TEMP_C, MAX_TARGET_TEMP_C, this.hass);
    return html`
      <div class="inline-warning">
        <ha-icon icon="mdi:alert-outline"></ha-icon>
        ${localize("schedule.block_temp_rejected", this.hass.language, {
          values: warnings.map((w) => this._formatWarning(w)).join(", "),
          min: range.min,
          max: range.max,
          unit: tempUnit(this.hass),
        })}
      </div>
    `;
  }

  /** "110 (Mon 00:00)" — weekday localised via Intl, so no extra locale keys. */
  private _formatWarning(w: ScheduleTempWarning): string {
    const index = WEEKDAYS.indexOf(w.day);
    let when = w.from.slice(0, 5);
    if (index >= 0) {
      // 2024-01-01 was a Monday, so +index lands on the right weekday.
      const date = new Date(Date.UTC(2024, 0, 1 + index));
      const weekday = new Intl.DateTimeFormat(this.hass.language, {
        weekday: "short",
        timeZone: "UTC",
      }).format(date);
      when = `${weekday} ${when}`;
    }
    return `${w.value} (${when})`;
  }

  // ─── Temperature inputs ────────────────────────────────────────

  private _renderTemperatureInputs(l: string) {
    if (this.climateMode === "auto") {
      return html`
        <div class="temp-grid-auto">
          <div class="temp-grid-header"></div>
          <div class="temp-grid-header">${localize("schedule.column_comfort", l)}</div>
          <div class="temp-grid-header">${localize("schedule.column_eco", l)}</div>
          <div class="temp-grid-row-label">
            <ha-icon icon="mdi:fire" style="--mdc-icon-size:16px"></ha-icon>
            ${localize("schedule.row_heat", l)}
          </div>
          <ha-textfield
            type="number"
            .value=${String(toDisplay(this.comfortHeat, this.hass))}
            suffix=${tempUnit(this.hass)}
            step=${tempStep(this.hass)}
            min=${tempRange(5, 35, this.hass).min}
            max=${tempRange(5, 35, this.hass).max}
            @change=${this._onComfortHeatChange}
          ></ha-textfield>
          <ha-textfield
            type="number"
            .value=${String(toDisplay(this.ecoHeat, this.hass))}
            suffix=${tempUnit(this.hass)}
            step=${tempStep(this.hass)}
            min=${tempRange(5, 35, this.hass).min}
            max=${tempRange(5, 35, this.hass).max}
            @change=${this._onEcoHeatChange}
          ></ha-textfield>
          <div class="temp-grid-row-label">
            <ha-icon icon="mdi:snowflake" style="--mdc-icon-size:16px"></ha-icon>
            ${localize("schedule.row_cool", l)}
          </div>
          <ha-textfield
            type="number"
            .value=${String(toDisplay(this.comfortCool, this.hass))}
            suffix=${tempUnit(this.hass)}
            step=${tempStep(this.hass)}
            min=${tempRange(5, 35, this.hass).min}
            max=${tempRange(5, 35, this.hass).max}
            @change=${this._onComfortCoolChange}
          ></ha-textfield>
          <ha-textfield
            type="number"
            .value=${String(toDisplay(this.ecoCool, this.hass))}
            suffix=${tempUnit(this.hass)}
            step=${tempStep(this.hass)}
            min=${tempRange(5, 35, this.hass).min}
            max=${tempRange(5, 35, this.hass).max}
            @change=${this._onEcoCoolChange}
          ></ha-textfield>
        </div>
      `;
    }

    return html`
      <div class="temp-inputs">
        <div class="temp-input-group">
          <ha-textfield
            type="number"
            label=${localize("schedule.comfort_label", l)}
            suffix=${tempUnit(this.hass)}
            step=${tempStep(this.hass)}
            .value=${String(
              toDisplay(
                this.climateMode === "cool_only" ? this.comfortCool : this.comfortHeat,
                this.hass,
              ),
            )}
            min=${tempRange(5, 35, this.hass).min}
            max=${tempRange(5, 35, this.hass).max}
            @change=${this.climateMode === "cool_only"
              ? this._onComfortCoolChange
              : this._onComfortHeatChange}
          ></ha-textfield>
        </div>
        <div class="temp-input-group">
          <ha-textfield
            type="number"
            label=${localize("schedule.eco_label", l)}
            suffix=${tempUnit(this.hass)}
            step=${tempStep(this.hass)}
            .value=${String(
              toDisplay(this.climateMode === "cool_only" ? this.ecoCool : this.ecoHeat, this.hass),
            )}
            min=${tempRange(5, 35, this.hass).min}
            max=${tempRange(5, 35, this.hass).max}
            @change=${this.climateMode === "cool_only"
              ? this._onEcoCoolChange
              : this._onEcoHeatChange}
          ></ha-textfield>
        </div>
      </div>
      <div class="fallback-hint">${localize("schedule.comfort_hint", l)}</div>
    `;
  }

  // ─── Selector value text ───────────────────────────────────────

  private _getSelectorValueText(l: string): string {
    const selectorState = this.hass?.states?.[this.scheduleSelectorEntity];
    if (!selectorState) return "";
    if (this.scheduleSelectorEntity.startsWith("input_boolean.")) {
      return localize("schedule.selector_value_boolean", l, {
        value: selectorState.state === "on" ? "On" : "Off",
      });
    }
    return localize("schedule.selector_value_number", l, {
      value: selectorState.state,
    });
  }

  // ─── Status text (temperature-specific) ────────────────────────

  /**
   * Read the active block's temperatures from the schedule entity's attributes.
   *
   * Implausible values are dropped by the backend (#395), so they must not be
   * shown as the active target here either. The *warning* about them comes from
   * `scheduleTempWarnings` instead — the backend scans the whole week, while
   * these attributes only ever describe the block running right now.
   */
  private _readBlockTemps(index: number): {
    blockTemp?: number;
    heatTemp?: number;
    coolTemp?: number;
  } {
    const entityState = this.hass?.states?.[this.schedules[index].entity_id];
    const attrs = entityState?.attributes ?? {};
    const usable = (v: unknown): number | undefined =>
      v != null && isPlausibleTargetTemp(v, this.hass) ? (v as number) : undefined;

    return {
      blockTemp: usable(attrs.temperature),
      heatTemp: usable(attrs.heat_temperature),
      coolTemp: usable(attrs.cool_temperature),
    };
  }

  private _getStatusText(index: number, state: "active" | "inactive" | "unreachable"): string {
    const l = this.hass.language;

    if (state === "unreachable") return localize("schedule.state_unreachable", l);
    if (state === "inactive") return localize("schedule.state_inactive", l);

    const schedule = this.schedules[index];
    const entityState = this.hass?.states?.[schedule.entity_id];
    if (!entityState) return localize("schedule.state_active", l);

    const isOn = entityState.state === "on";
    if (isOn) {
      const { blockTemp, heatTemp, coolTemp } = this._readBlockTemps(index);
      if (blockTemp != null) {
        return localize("schedule.from_schedule", l, {
          temp: String(blockTemp),
          unit: tempUnit(this.hass),
        });
      }
      if (heatTemp != null || coolTemp != null) {
        // heatTemp/coolTemp are entity attributes (already display units); the
        // comfort fallbacks are backend Celsius and need converting.
        return localize("schedule.from_schedule_split", l, {
          heat: heatTemp != null ? String(heatTemp) : formatTemp(this.comfortHeat, this.hass),
          cool: coolTemp != null ? String(coolTemp) : formatTemp(this.comfortCool, this.hass),
          unit: tempUnit(this.hass),
        });
      }
      return localize("schedule.fallback", l, {
        temp: formatTemp(
          this.climateMode === "cool_only" ? this.comfortCool : this.comfortHeat,
          this.hass,
        ),
        unit: tempUnit(this.hass),
      });
    }

    return localize("schedule.eco_detail", l, {
      temp: formatTemp(this.climateMode === "cool_only" ? this.ecoCool : this.ecoHeat, this.hass),
      unit: tempUnit(this.hass),
    });
  }

  // ─── Schedule management ───────────────────────────────────────

  private _addSchedule(entityId: string) {
    this._emitSchedules([...this.schedules, { entity_id: entityId }]);
  }

  private _removeSchedule(index: number) {
    this._emitSchedules(this.schedules.filter((_, i) => i !== index));
  }

  private _moveSchedule(index: number, direction: -1 | 1) {
    const target = index + direction;
    if (target < 0 || target >= this.schedules.length) return;
    const next = [...this.schedules];
    [next[index], next[target]] = [next[target], next[index]];
    this._emitSchedules(next);
  }

  private _emitSchedules(value: ScheduleEntry[]) {
    this.dispatchEvent(
      new CustomEvent("schedules-changed", {
        detail: { value },
        bubbles: true,
        composed: true,
      }),
    );
  }

  private _onSelectorEntityChange(value: string) {
    this.dispatchEvent(
      new CustomEvent("schedule-selector-changed", {
        detail: { value },
        bubbles: true,
        composed: true,
      }),
    );
  }

  // ─── Temperature change handlers ──────────────────────────────

  private _onComfortHeatChange(e: Event) {
    const target = e.target as HTMLElement & { value: string };
    const val = toCelsius(parseFloat(target.value) || toDisplay(21.0, this.hass), this.hass);
    this.dispatchEvent(
      new CustomEvent("comfort-heat-changed", {
        detail: { value: val },
        bubbles: true,
        composed: true,
      }),
    );
    if (this.comfortCool < val) {
      this.dispatchEvent(
        new CustomEvent("comfort-cool-changed", {
          detail: { value: val },
          bubbles: true,
          composed: true,
        }),
      );
    }
  }

  private _onComfortCoolChange(e: Event) {
    const target = e.target as HTMLElement & { value: string };
    const val = toCelsius(parseFloat(target.value) || toDisplay(24.0, this.hass), this.hass);
    this.dispatchEvent(
      new CustomEvent("comfort-cool-changed", {
        detail: { value: val },
        bubbles: true,
        composed: true,
      }),
    );
    if (this.comfortHeat > val) {
      this.dispatchEvent(
        new CustomEvent("comfort-heat-changed", {
          detail: { value: val },
          bubbles: true,
          composed: true,
        }),
      );
    }
  }

  private _onEcoHeatChange(e: Event) {
    const target = e.target as HTMLElement & { value: string };
    const val = toCelsius(parseFloat(target.value) || toDisplay(17.0, this.hass), this.hass);
    this.dispatchEvent(
      new CustomEvent("eco-heat-changed", {
        detail: { value: val },
        bubbles: true,
        composed: true,
      }),
    );
    if (this.ecoCool < val) {
      this.dispatchEvent(
        new CustomEvent("eco-cool-changed", {
          detail: { value: val },
          bubbles: true,
          composed: true,
        }),
      );
    }
  }

  private _onEcoCoolChange(e: Event) {
    const target = e.target as HTMLElement & { value: string };
    const val = toCelsius(parseFloat(target.value) || toDisplay(27.0, this.hass), this.hass);
    this.dispatchEvent(
      new CustomEvent("eco-cool-changed", {
        detail: { value: val },
        bubbles: true,
        composed: true,
      }),
    );
    if (this.ecoHeat > val) {
      this.dispatchEvent(
        new CustomEvent("eco-heat-changed", {
          detail: { value: val },
          bubbles: true,
          composed: true,
        }),
      );
    }
  }
}

declare global {
  interface HTMLElementTagNameMap {
    "rs-schedule-settings": RsScheduleSettings;
  }
}
