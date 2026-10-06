<template>
	<div class="am-root">
		<!-- Toolbar -->
		<div class="am-toolbar">
			<div class="am-dategroup">
				<button class="am-nav" :title="__('Previous day')" @click="shiftDay(-1)">&lsaquo;</button>
				<button class="am-datebtn" @click="openPicker">
					{{ displayDate }}
					<input
						ref="picker"
						v-model="date"
						type="date"
						class="am-date-native"
						@change="load"
					/>
				</button>
				<button class="am-nav" :title="__('Next day')" @click="shiftDay(1)">&rsaquo;</button>
				<button class="am-today" :disabled="isToday" @click="goToday">{{ __("Today") }}</button>
			</div>
			<input
				v-model.trim="search"
				type="search"
				class="am-search"
				:placeholder="__('Search employees…')"
			/>
			<select v-if="departments.length > 1" v-model="department" class="am-select">
				<option value="">{{ __("All departments") }}</option>
				<option v-for="d in departments" :key="d" :value="d">{{ d }}</option>
			</select>
			<select v-if="companies.length > 1" v-model="company" class="am-select" @change="onCompanyChange">
				<option v-for="c in companies" :key="c" :value="c">{{ c }}</option>
			</select>
		</div>

		<!-- Holiday banner -->
		<div v-if="holiday" class="am-holiday-banner">
			<Icon name="calendar" /> {{ __("Holiday") }}: {{ holiday }}
		</div>

		<!-- Summary chips (click to filter the rows to that group; click again to clear) -->
		<div class="am-chips">
			<button class="am-chip" :class="{ 'am-chip-active': chipFilter === 'scanned' }" @click="toggleChip('scanned')">{{ scannedCount }} {{ __("scanned") }}</button>
			<button class="am-chip am-chip-flag" :class="{ 'am-chip-active': chipFilter === 'flagged' }" v-if="flaggedCount" @click="toggleChip('flagged')"><Icon name="alert" size="12" /> {{ flaggedCount }} {{ __("flagged") }}</button>
			<button class="am-chip am-chip-now" :class="{ 'am-chip-active': chipFilter === 'on_site' }" v-if="isToday" @click="toggleChip('on_site')"><Icon name="dot" size="12" /> {{ onSiteCount }} {{ __("on site now") }}</button>
			<button class="am-chip am-chip-leave" :class="{ 'am-chip-active': chipFilter === 'on_leave' }" v-if="leaveCount" @click="toggleChip('on_leave')"><Icon name="umbrella" size="12" /> {{ leaveCount }} {{ __("on leave") }}</button>
			<button class="am-chip am-chip-absent" :class="{ 'am-chip-active': chipFilter === 'no_punches' }" v-if="absentCount" @click="toggleChip('no_punches')"><Icon name="minus" size="12" /> {{ absentCount }} {{ __("no punches") }}</button>
		</div>

		<!-- Rows -->
		<div v-if="loading" class="am-empty">{{ __("Loading…") }}</div>
		<div v-else-if="!filteredRows.length" class="am-empty">
			{{ __("No punches for this day.") }}
		</div>
		<div v-else class="am-list" :style="{ '--head-w': headWidth + 'px' }">
			<!-- Hour ruler -->
			<div class="am-row am-ruler-row">
				<div class="am-rowhead">
					<span class="am-headlabel">{{ __("Employee") }}</span>
					<span class="am-resizer" @mousedown.prevent="startResize"></span>
				</div>
				<div class="am-ruler">
					<span
						v-for="h in rulerHours"
						:key="h"
						class="am-ruler-tick"
						:style="{ left: pct(hourToMs(h)) }"
						>{{ h }}:00</span
					>
				</div>
			</div>

			<div
				v-for="row in filteredRows"
				:key="row.employee + row.date"
				class="am-row"
				:class="{ 'am-row-absent': isEmptyRow(row) }"
			>
				<div class="am-rowhead">
					<div class="am-name" :title="row.employee">{{ row.employee_name }}</div>
					<div class="am-hours">
						<template v-if="!isEmptyRow(row)">
							<span class="am-work">{{ fmtH(row.work_hours) }}</span>
							<span class="am-break" v-if="row.break_hours"><Icon name="coffee" size="12" /> {{ fmtH(row.break_hours) }}</span>
							<span v-if="row.flag" class="am-flagicon" :title="flagLabel(row.flag)"><Icon name="alert" /></span>
							<span v-else-if="onSite(row)" class="am-nowicon" :title="__('On site now')"><Icon name="dot" size="11" /></span>
							<span v-else class="am-okicon" :title="__('Complete')"><Icon name="check" /></span>
						</template>
						<span v-else-if="row.flag === 'on_leave'" class="am-leavetag"><Icon name="umbrella" size="13" /> {{ row.leave_type }}</span>
						<span v-else class="am-absenticon"><Icon name="minus" /></span>
					</div>
				</div>
				<TimelineRow
					:row="row"
					:date="date"
					:scale-min="scale.min"
					:scale-span="scale.span"
					:is-today="isToday"
					:now-ms-abs="isToday && nowInScale ? nowMs : -1"
					:readonly="!canCorrect"
					:saving="!!savingKeys[row.employee]"
					@add="(p) => openAdd(row, p)"
					@edit="(p) => openEdit(row, p.checkin, p)"
					@dragsave="(p) => onDragSave(row, p)"
				/>
			</div>
		</div>

		<PunchDialog
			v-if="dialog"
			:mode="dialog.mode"
			:employee-name="dialog.employeeName"
			:date="date"
			:prefill="dialog.prefill"
			:checkin-name="dialog.checkinName"
			:busy="saving"
			:x="dialog.x"
			:y="dialog.y"
			@save="onSave"
			@remove="onRemove"
			@close="dialog = null"
		/>
	</div>
</template>

<script>
import PunchDialog from "./PunchDialog.vue";
import TimelineRow from "./TimelineRow.vue";
import Icon from "./Icon.vue";
import * as api from "./api.js";

export default {
	name: "AttendanceMonitorApp",
	components: { PunchDialog, TimelineRow, Icon },
	data() {
		// The timeline axis is the SITE's wall clock (punches are site-local naive
		// datetimes). So "today" and the now-line must use the site timezone, not
		// the viewer's browser zone (e.g. an Amsterdam site viewed from Dhaka).
		const siteTz =
			(typeof frappe !== "undefined" && frappe.sys_defaults && frappe.sys_defaults.time_zone) ||
			(typeof frappe !== "undefined" && frappe.boot && frappe.boot.time_zone &&
				(frappe.boot.time_zone.system || frappe.boot.time_zone)) ||
			Intl.DateTimeFormat().resolvedOptions().timeZone;
		const parts = new Intl.DateTimeFormat("en-GB", {
			timeZone: siteTz, year: "numeric", month: "2-digit", day: "2-digit",
		}).formatToParts(new Date());
		const g = (t) => parts.find((x) => x.type === t).value;
		const today = `${g("year")}-${g("month")}-${g("day")}`;
		return {
			siteTz,
			date: today,
			company: "VGH B.V.",
			companies: [],
			department: "",
			search: "",
			chipFilter: "", // "" | scanned | flagged | on_site | on_leave | no_punches
			rows: [],
			loading: false,
			saving: false,
			savingKeys: {}, // employee -> true while an optimistic correction is in flight
			dialog: null,
			nowTick: Date.now(),
			headWidth: Number(localStorage.getItem("am_head_w")) || 230,
			canCorrect: false,
		};
	},
	computed: {
		isToday() {
			return this.date === this.localToday();
		},
		displayDate() {
			const [y, m, d] = this.date.split("-").map(Number);
			const mon = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"][m - 1];
			return `${String(d).padStart(2, "0")} ${mon} ${y}`;
		},
		departments() {
			return [...new Set(this.rows.map((r) => r.department).filter(Boolean))].sort();
		},
		filteredRows() {
			const q = this.search.toLowerCase();
			// Always sort by employee name so a row keeps its position when it
			// changes from absent→scanned (adding a punch no longer makes it jump).
			return this.rows
				.filter(
					(r) =>
						(!q || r.employee_name.toLowerCase().includes(q)) &&
						(!this.department || r.department === this.department) &&
						(!this.chipFilter || this.chipMatch(r, this.chipFilter))
				)
				.slice()
				.sort((a, b) => a.employee_name.localeCompare(b.employee_name));
		},
		holiday() {
			return (this.rows[0] && this.rows[0].holiday) || "";
		},
		scannedCount() {
			return this.rows.filter((r) => r.checkins.length).length;
		},
		flaggedCount() {
			return this.rows.filter((r) => r.flag === "missing_punch" || r.flag === "unexpected_count").length;
		},
		leaveCount() {
			return this.rows.filter((r) => r.flag === "on_leave").length;
		},
		absentCount() {
			return this.rows.filter((r) => r.flag === "no_punches").length;
		},
		onSiteCount() {
			return this.rows.filter((r) => this.onSite(r)).length;
		},
		// Timeline scale: floor/ceil to the hour around all scans, min 4h span.
		scale() {
			let min = Infinity;
			let max = -Infinity;
			for (const r of this.rows) {
				for (const c of [...r.checkins, ...(r.superseded || [])]) {
					const t = this.toMs(c.time);
					if (t < min) min = t;
					if (t > max) max = t;
				}
			}
			if (!isFinite(min)) {
				min = this.hourToMs(6);
				max = this.hourToMs(18);
			}
			min = Math.floor(min / 3600000) * 3600000;
			max = Math.ceil(max / 3600000) * 3600000;
			if (this.isToday) max = Math.max(max, Math.ceil(this.nowMsAbs() / 3600000) * 3600000);
			if (max - min < 4 * 3600000) max = min + 4 * 3600000;
			return { min, max, span: max - min };
		},
		rulerHours() {
			const startH = Math.round(this.scale.min / 3600000);
			const n = Math.round(this.scale.span / 3600000);
			const step = n > 10 ? 2 : 1;
			const hours = [];
			for (let i = 0; i <= n; i += step) hours.push(startH + i);
			return hours;
		},
		nowMs() {
			return this.nowMsAbs();
		},
		nowInScale() {
			const t = this.nowMsAbs();
			return t >= this.scale.min && t <= this.scale.max;
		},
	},
	mounted() {
		// Bootstrap: permitted companies + whether corrections are allowed, so a
		// restricted user never queries a forbidden company and the UI reflects
		// the site's view-only setting.
		api.fetchConfig().then((cfg) => {
			this.canCorrect = !!(cfg && cfg.can_correct);
			const list = (cfg && cfg.companies) || [];
			this.companies = list;
			if (list.length && !list.includes(this.company)) {
				this.company = list[0];
			}
			this.load();
		});
		this._timer = setInterval(() => (this.nowTick = Date.now()), 60000);
	},
	beforeUnmount() {
		clearInterval(this._timer);
	},
	methods: {
		__(s) {
			return typeof __ !== "undefined" ? __(s) : s;
		},
		async load() {
			this.loading = !this.rows.length; // quiet refresh: keep rows on screen
			try {
				this.rows = (await api.fetchMonitor({
					from_date: this.date,
					to_date: this.date,
					company: this.company,
					include_absent: 1,
				})) || [];
			} finally {
				this.loading = false;
			}
		},
		localToday() {
			return this.siteNow().ymd;
		},
		fmtDate(y, m, d) {
			return `${y}-${String(m).padStart(2, "0")}-${String(d).padStart(2, "0")}`;
		},
		shiftDay(n) {
			// build the date from LOCAL parts - toISOString() would shift by the
			// UTC offset (CEST) and eat the increment.
			const [y, m, d] = this.date.split("-").map(Number);
			const nd = new Date(y, m - 1, d + n);
			this.date = this.fmtDate(nd.getFullYear(), nd.getMonth() + 1, nd.getDate());
			this.load();
		},
		goToday() {
			this.date = this.localToday();
			this.load();
		},
		openPicker() {
			const el = this.$refs.picker;
			if (el && el.showPicker) {
				try {
					el.showPicker();
					return;
				} catch (e) {
					/* fall through */
				}
			}
			el && el.focus();
		},
		onCompanyChange() {
			this.department = "";
			this.load();
		},
		// --- resizable name column ---
		startResize(ev) {
			const startX = ev.clientX;
			const startW = this.headWidth;
			const move = (e) => {
				this.headWidth = Math.max(140, Math.min(520, startW + (e.clientX - startX)));
			};
			const up = () => {
				window.removeEventListener("mousemove", move);
				window.removeEventListener("mouseup", up);
				document.body.classList.remove("am-resizing");
				localStorage.setItem("am_head_w", String(this.headWidth));
			};
			window.addEventListener("mousemove", move);
			window.addEventListener("mouseup", up);
			document.body.classList.add("am-resizing");
		},
		// --- time helpers (all relative to selected date, in ms) ---
		toMs(iso) {
			return new Date(iso).getTime() - new Date(this.date + "T00:00:00").getTime();
		},
		hourToMs(h) {
			return h * 3600000;
		},
		// Current site wall-clock time as {ymd, ms-since-site-midnight}. Reactive on
		// nowTick so the now-line advances. Read via Intl in the site timezone.
		siteNow() {
			this.nowTick; // reactive dependency
			const p = new Intl.DateTimeFormat("en-GB", {
				timeZone: this.siteTz, year: "numeric", month: "2-digit", day: "2-digit",
				hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false,
			}).formatToParts(new Date());
			const g = (t) => p.find((x) => x.type === t).value;
			let hh = parseInt(g("hour"), 10);
			if (hh === 24) hh = 0; // some engines emit "24" at midnight
			return {
				ymd: `${g("year")}-${g("month")}-${g("day")}`,
				ms: ((hh * 60 + parseInt(g("minute"), 10)) * 60 + parseInt(g("second"), 10)) * 1000,
			};
		},
		nowMsAbs() {
			// Only meaningful when the selected date is the site's today (template
			// gates with isToday); then now sits at its site wall-clock position.
			return this.siteNow().ms;
		},
		pct(ms) {
			const p = ((ms - this.scale.min) / this.scale.span) * 100;
			return Math.max(0, Math.min(100, p)) + "%";
		},
		fmtH(h) {
			// Frappe's Duration style: "8h 30m", "30m", "0m"
			const secs = Math.round((h || 0) * 3600);
			if (secs <= 0) return "0m";
			return frappe.utils.get_formatted_duration(secs, { hide_days: 1, hide_seconds: 1 }) || "0m";
		},
		flagLabel(flag) {
			return flag === "missing_punch"
				? this.__("Missing punch · odd number of scans")
				: this.__("Unexpected number of scans");
		},
		onSite(row) {
			return this.isToday && row.checkins.length % 2 === 1;
		},
		// Predicate behind each summary chip; keep in sync with the *Count computeds.
		chipMatch(row, key) {
			switch (key) {
				case "scanned": return !!row.checkins.length;
				case "flagged": return row.flag === "missing_punch" || row.flag === "unexpected_count";
				case "on_site": return this.onSite(row);
				case "on_leave": return row.flag === "on_leave";
				case "no_punches": return row.flag === "no_punches";
				default: return true;
			}
		},
		toggleChip(key) {
			this.chipFilter = this.chipFilter === key ? "" : key;
		},
		isEmptyRow(row) {
			return !row.checkins.length;
		},
		// --- corrections ---
		openAdd(row, { time, x, y }) {
			this.dialog = {
				mode: "add",
				employee: row.employee,
				employeeName: row.employee_name,
				prefill: time,
				checkinName: null,
				x,
				y,
			};
		},
		openEdit(row, checkin, pos = {}) {
			this.dialog = {
				mode: "edit",
				employee: row.employee,
				employeeName: row.employee_name,
				prefill: checkin.time.slice(11, 16),
				checkinName: checkin.name,
				x: pos.x || window.innerWidth / 2,
				y: pos.y || 120,
			};
		},
		// Replace one row in place (preserving list order) - used to silently
		// reconcile with the server's recomputed row after a correction, with no
		// full reload / reflow / reorder.
		patchRow(updated) {
			if (!updated || !updated.employee) return;
			const i = this.rows.findIndex((r) => r.employee === updated.employee && r.date === updated.date);
			if (i >= 0) this.rows.splice(i, 1, updated);
		},
		setSaving(employee, on) {
			if (on) this.savingKeys = { ...this.savingKeys, [employee]: true };
			else {
				const next = { ...this.savingKeys };
				delete next[employee];
				this.savingKeys = next;
			}
		},
		notifyError(e) {
			const msg = (e && (e.message || (e._server_messages && e._server_messages))) || this.__("Could not save the change");
			if (typeof frappe !== "undefined" && frappe.show_alert) {
				frappe.show_alert({ message: String(msg), indicator: "red" }, 5);
			}
		},
		async onDragSave(row, { name, time }) {
			// Optimistic: move the punch locally NOW (synchronously, before any
			// await) so the dragged handle stays where it was dropped instead of
			// snapping back to the old server time while the save round-trips.
			const checkin = row.checkins.find((c) => c.name === name);
			const original = checkin ? checkin.time : null;
			if (checkin) checkin.time = `${this.date}T${time}:00`;
			this.setSaving(row.employee, true);
			try {
				const updated = await api.updateCheckin({ name, time: `${this.date} ${time}:00` });
				this.patchRow(updated); // silent reconcile (recomputed totals/segments)
			} catch (e) {
				if (checkin && original !== null) checkin.time = original; // revert on failure
				this.notifyError(e);
			} finally {
				this.setSaving(row.employee, false);
			}
		},
		async onSave({ name, time }) {
			const employee = this.dialog.employee;
			const isAdd = this.dialog.mode === "add";
			this.saving = true;
			try {
				const updated = isAdd
					? await api.addCheckin({ employee, time })
					: await api.updateCheckin({ name, time });
				this.dialog = null;
				this.patchRow(updated); // patch in place - no reflow
			} catch (e) {
				this.notifyError(e);
			} finally {
				this.saving = false;
			}
		},
		async onRemove({ name }) {
			this.saving = true;
			try {
				const updated = await api.deleteCheckin({ name });
				this.dialog = null;
				this.patchRow(updated);
			} catch (e) {
				this.notifyError(e);
			} finally {
				this.saving = false;
			}
		},
	},
};
</script>

<style>
/* not scoped: global cursor while resizing the name column */
body.am-resizing,
body.am-resizing * {
	cursor: col-resize !important;
	user-select: none !important;
}
</style>

<style scoped>
.am-root {
	padding: 12px 16px 48px;
	width: 100%;
}
.am-toolbar {
	display: flex;
	flex-wrap: wrap;
	gap: 10px;
	align-items: center;
	margin-bottom: 10px;
}
.am-dategroup {
	display: flex;
	gap: 6px;
	align-items: center;
}
.am-nav,
.am-today {
	border: 1px solid var(--border-color, #d1d8dd);
	background: var(--control-bg, #f4f5f6);
	border-radius: 8px;
	min-width: 38px;
	min-height: 36px;
	font-size: 16px;
	cursor: pointer;
}
.am-today {
	font-size: 13px;
	padding: 0 10px;
}
.am-datebtn {
	position: relative;
	border: 1px solid var(--border-color, #d1d8dd);
	background: var(--control-bg, #fff);
	border-radius: 8px;
	padding: 7px 12px;
	font-size: 13px;
	min-height: 36px;
	min-width: 118px;
	cursor: pointer;
	font-variant-numeric: tabular-nums;
}
.am-date-native {
	position: absolute;
	inset: 0;
	opacity: 0;
	pointer-events: none;
	width: 100%;
}
.am-search,
.am-select {
	border: 1px solid var(--border-color, #d1d8dd);
	border-radius: 8px;
	padding: 7px 10px;
	font-size: 13px;
	min-height: 36px;
	background: var(--control-bg, #fff);
}
.am-search {
	flex: 1;
	min-width: 140px;
}
.am-chips {
	display: flex;
	gap: 8px;
	margin: 2px 0 18px;
	flex-wrap: wrap;
}
.am-chip {
	background: var(--control-bg, #f4f5f6);
	border: 1px solid transparent;
	border-radius: 20px;
	padding: 4px 12px;
	font-size: 12px;
	cursor: pointer;
	display: inline-flex;
	align-items: center;
	gap: 4px;
	transition: filter 0.12s, box-shadow 0.12s;
	color: inherit;
}
.am-chip:hover {
	filter: brightness(0.96);
}
/* active filter: ring in the chip's own colour so it reads as "on" */
.am-chip-active {
	border-color: currentColor;
	box-shadow: 0 0 0 1px currentColor inset;
	font-weight: 600;
}
.am-chip-flag {
	background: #fdf0e7;
	color: #b45309;
}
.am-chip-now {
	background: #e8f6ee;
	color: #1e7e46;
}
.am-empty {
	color: var(--text-muted, #6c7680);
	padding: 40px 0;
	text-align: center;
}
.am-row {
	display: flex;
	align-items: center;
	gap: 14px;
	padding: 9px 6px;
	border-bottom: 1px solid var(--border-color, #ebeef0);
	border-radius: 8px;
	transition: background 0.15s;
}
.am-row:hover {
	background: rgba(100, 116, 139, 0.05);
}
.am-rowhead {
	position: relative;
	width: var(--head-w, 230px);
	min-width: var(--head-w, 230px);
	display: flex;
	justify-content: space-between;
	align-items: baseline;
	gap: 8px;
}
.am-headlabel {
	font-size: 11px;
	font-weight: 600;
	text-transform: uppercase;
	letter-spacing: 0.04em;
	color: var(--text-muted, #98a1a9);
}
.am-resizer {
	position: absolute;
	right: -8px;
	top: -4px;
	bottom: -4px;
	width: 12px;
	cursor: col-resize;
	z-index: 5;
}
.am-resizer::after {
	content: "";
	position: absolute;
	left: 5px;
	top: 4px;
	bottom: 4px;
	width: 2px;
	background: var(--border-color, #cdd5db);
	border-radius: 2px;
}
.am-resizer:hover::after {
	background: #1f6fd6;
}
.am-name {
	font-weight: 500;
	font-size: 13px;
	overflow: hidden;
	text-overflow: ellipsis;
	white-space: nowrap;
}
.am-hours {
	font-size: 12px;
	display: flex;
	gap: 8px;
	align-items: center;
	white-space: nowrap;
}
.am-hours > span {
	display: inline-flex;
	align-items: center;
	gap: 3px;
}
.am-work {
	font-weight: 600;
}
.am-break {
	color: var(--text-muted, #7a848c);
}
.am-flagicon {
	color: #b8791f; /* restrained amber - the one thing worth noticing */
}
.am-nowicon {
	color: #5a9c7a; /* muted green */
}
.am-okicon {
	color: #b6bfc6;
}
.am-absenticon {
	color: #b6bfc6;
}
.am-leavetag {
	color: var(--text-muted, #7a848c);
}
.am-row-absent .am-name {
	color: var(--text-muted, #98a1a9);
	font-weight: 400;
}
.am-chip-absent {
	background: #f2f4f6;
	color: #8a949c;
	border: 1px dashed #c9d1d8;
}
.am-chip-leave {
	background: var(--control-bg, #f4f5f6);
	color: var(--text-muted, #6c7680);
}
.am-holiday-banner {
	display: flex;
	align-items: center;
	gap: 7px;
	background: var(--control-bg, #f6f7f8);
	border: 1px solid var(--border-color, #e3e7ea);
	color: var(--text-muted, #6c7680);
	border-radius: 8px;
	padding: 7px 12px;
	font-size: 13px;
	margin-bottom: 12px;
}
.am-ruler-row {
	border-bottom: none;
	padding-bottom: 0;
}
.am-ruler {
	position: relative;
	flex: 1;
	height: 16px;
}
.am-ruler-tick {
	position: absolute;
	transform: translateX(-50%);
	font-size: 10px;
	color: var(--text-muted, #98a1a9);
}

/* --- Mobile: stack the row, larger touch targets --- */
@media (max-width: 640px) {
	.am-row {
		flex-direction: column;
		align-items: stretch;
		gap: 4px;
		padding: 10px 0;
	}
	.am-rowhead {
		width: 100%;
		min-width: 0;
	}
	.am-ruler-row {
		display: none;
	}
}
</style>
