<template>
	<div
		class="tr-bar"
		:class="{ 'tr-bar-flagged': row.flag === 'missing_punch', 'tr-bar-empty': isEmpty, 'tr-live': drag, 'tr-readonly': readonly, 'tr-saving': saving }"
		ref="bar"
		@mousemove="onHover"
		@mouseleave="hoverMs = null"
		@click="onBarClick"
	>
		<span v-if="isEmpty && !hoverMs" class="tr-empty-label">
			<Icon :name="emptyIcon" size="13" /> {{ emptyText }}
		</span>

		<!-- segments (work / break / unknown) - derived live from marker positions -->
		<div
			v-for="(s, i) in segments"
			:key="'s' + i"
			class="tr-seg"
			:class="'tr-seg-' + s.type"
			:style="{ left: pct(s.a), width: `calc(${pct(s.b)} - ${pct(s.a)})` }"
		>
			<span v-if="segWide(s)" class="tr-seg-dur">{{ dur(s.b - s.a) }}</span>
		</div>

		<!-- punches a shift correction replaced: shown greyed, never counted or edited -->
		<div
			v-for="g in supersededMarks"
			:key="g.name"
			class="tr-mark tr-mark-superseded"
			:style="{ left: pct(g.ms) }"
			:title="fmt(g.ms) + ' · ' + t('replaced by') + ' ' + g.by"
			@mousedown.prevent.stop
			@click.stop="explain(g)"
		></div>

		<!-- IN/OUT trim handles (a punch from an Attendance Request is greyed and locked) -->
		<div
			v-for="m in markers"
			:key="m.name"
			class="tr-mark"
			:class="['tr-mark-' + m.io, { 'tr-mark-drag': drag && drag.name === m.name, 'tr-mark-locked': m.request }]"
			:style="{ left: pct(m.ms) }"
			:title="fmt(m.ms) + ' · ' + markLabel(m.io) + (m.request ? ' · ' + m.request : '')"
			@mousedown.prevent.stop="startDrag(m, $event)"
			@click.stop="m.request && explain(m)"
		></div>

		<!-- hover ghost -->
		<template v-if="hoverMs !== null && !drag">
			<div class="tr-ghost" :style="{ left: pct(hoverMs) }"></div>
			<div class="tr-pill" :style="pillStyle(hoverMs)">+ {{ fmt(hoverMs) }}</div>
		</template>

		<!-- drag feedback -->
		<template v-if="drag">
			<div class="tr-ghost tr-ghost-drag" :style="{ left: pct(drag.curMs) }"></div>
			<div class="tr-pill tr-pill-drag" :style="pillStyle(drag.curMs)">
				{{ fmt(drag.curMs) }} · {{ liveTotals }}
			</div>
		</template>

		<div v-if="isToday && nowMs >= 0 && nowMs <= scaleSpan" class="tr-nowline" :style="{ left: pct(nowMsAbs) }"></div>
	</div>
</template>

<script>
import Icon from "./Icon.vue";

const SNAP = 300000; // 5 minutes

export default {
	name: "TimelineRow",
	components: { Icon },
	props: {
		row: { type: Object, required: true },
		date: { type: String, required: true },
		scaleMin: { type: Number, required: true },
		scaleSpan: { type: Number, required: true },
		isToday: { type: Boolean, default: false },
		nowMsAbs: { type: Number, default: -1 },
		readonly: { type: Boolean, default: false },
		saving: { type: Boolean, default: false },
	},
	emits: ["add", "edit", "dragsave"],
	data() {
		return { hoverMs: null, drag: null, suppressClick: false };
	},
	computed: {
		isEmpty() {
			return !this.row.checkins.length;
		},
		emptyIcon() {
			if (this.row.flag === "on_leave") return "umbrella";
			if (this.row.holiday) return "calendar";
			return "plus";
		},
		emptyText() {
			if (this.row.flag === "on_leave") {
				return this.t("On leave") + (this.row.leave_type ? " · " + this.row.leave_type : "");
			}
			if (this.row.holiday) return this.t("Holiday");
			return this.t("no punches · click to add");
		},
		inLabel() {
			return this.t("in");
		},
		outLabel() {
			return this.t("out");
		},
		nowMs() {
			return this.nowMsAbs - this.scaleMin;
		},
		// Single source of truth: every punch as {name, ms, io}, sorted by time,
		// with the dragged one at its live position. Segments + totals derive
		// from this, so the whole bar moves together during a drag (no jump).
		isSpan() {
			// First-in / last-out shift: whole span is worked, no breaks. Only the
			// first and last punch bound the time; the ones between are informational.
			return this.row.mode === "span";
		},
		markers() {
			const list = this.row.checkins.map((c) => ({
				name: c.name,
				request: c.request || null,
				ms: this.drag && this.drag.name === c.name ? this.drag.curMs : this.toMs(c.time),
			}));
			list.sort((a, b) => a.ms - b.ms);
			if (this.isSpan) {
				// first = in, last = out, everything between = a dimmed "mid" punch
				list.forEach((m, i) => {
					m.io = i === 0 ? "in" : i === list.length - 1 ? "out" : "mid";
				});
				if (list.length === 1) list[0].io = "in";
			} else {
				list.forEach((m, i) => (m.io = i % 2 === 0 ? "in" : "out"));
			}
			return list;
		},
		supersededMarks() {
			return (this.row.superseded || []).map((c) => ({
				name: c.name,
				by: c.superseded_by,
				ms: this.toMs(c.time),
			}));
		},
		segments() {
			const t = this.markers.map((m) => m.ms);
			if (this.isSpan) {
				// one continuous work bar first->last, no break/unknown
				return t.length >= 2 ? [{ type: "work", a: t[0], b: t[t.length - 1] }] : [];
			}
			const segs = [];
			let i = 0;
			for (; i + 1 < t.length; i += 2) {
				segs.push({ type: "work", a: t[i], b: t[i + 1] });
				if (i + 3 < t.length) segs.push({ type: "break", a: t[i + 1], b: t[i + 2] });
			}
			// odd trailing punch → unclassifiable stretch
			if (t.length >= 3 && t.length % 2 === 1) {
				segs.push({ type: "unknown", a: t[t.length - 2], b: t[t.length - 1] });
			}
			return segs;
		},
		liveTotals() {
			const t = this.markers.map((m) => m.ms);
			if (this.isSpan) {
				const work = t.length >= 2 ? t[t.length - 1] - t[0] : 0;
				return `${this.dur(work)} ${this.t("work")}`;
			}
			let work = 0;
			let brk = 0;
			for (let i = 0; i + 1 < t.length; i += 2) {
				work += t[i + 1] - t[i];
				if (i + 3 < t.length) brk += t[i + 2] - t[i + 1];
			}
			return brk
				? `${this.dur(work)} ${this.t("work")} · ${this.dur(brk)} ${this.t("break")}`
				: `${this.dur(work)} ${this.t("work")}`;
		},
	},
	beforeUnmount() {
		this.unbindDrag();
	},
	methods: {
		t(s) {
			return typeof __ !== "undefined" ? __(s) : s;
		},
		explain(mark) {
			const message = mark.by
				? __("Replaced by the correction in Attendance Request {0}; it no longer counts. Cancel or amend that request to change the shift.", [mark.by])
				: __("From Attendance Request {0}. Cancel or amend that request to change it.", [mark.request]);
			frappe.show_alert({ message, indicator: "orange" }, 6);
		},
		markLabel(io) {
			if (io === "in") return this.inLabel;
			if (io === "out") return this.outLabel;
			return this.t("punch");
		},
		toMs(iso) {
			return new Date(iso).getTime() - new Date(this.date + "T00:00:00").getTime();
		},
		pct(ms) {
			const p = ((ms - this.scaleMin) / this.scaleSpan) * 100;
			return Math.max(0, Math.min(100, p)) + "%";
		},
		fmt(ms) {
			return `${String(Math.floor(ms / 3600000)).padStart(2, "0")}:${String(Math.floor((ms % 3600000) / 60000)).padStart(2, "0")}`;
		},
		dur(ms) {
			// Frappe Duration style: "8h 30m", "30m", "0m"
			const secs = Math.round((ms || 0) / 1000);
			if (secs <= 0) return "0m";
			return frappe.utils.get_formatted_duration(secs, { hide_days: 1, hide_seconds: 1 }) || "0m";
		},
		segWide(s) {
			// only label segments wide enough to fit the text (avoid clutter)
			return ((s.b - s.a) / this.scaleSpan) * 100 > 7;
		},
		pillStyle(ms) {
			const p = ((ms - this.scaleMin) / this.scaleSpan) * 100;
			if (p < 8) return { left: p + "%", transform: "translateX(0)" };
			if (p > 92) return { left: p + "%", transform: "translateX(-100%)" };
			return { left: p + "%", transform: "translateX(-50%)" };
		},
		eventMs(ev) {
			const rect = this.$refs.bar.getBoundingClientRect();
			const frac = Math.max(0, Math.min(1, (ev.clientX - rect.left) / rect.width));
			return Math.round((this.scaleMin + frac * this.scaleSpan) / SNAP) * SNAP;
		},
		onHover(ev) {
			if (this.drag || this.readonly || this.saving) return;
			this.hoverMs = ev.target.classList.contains("tr-mark") ? null : this.eventMs(ev);
		},
		onBarClick(ev) {
			if (this.suppressClick || this.readonly || this.saving) return;
			this.$emit("add", { time: this.fmt(this.eventMs(ev)), x: ev.clientX, y: ev.clientY });
		},
		// ---- drag ----
		startDrag(m, ev) {
			// A punch from an Attendance Request changes only through that request.
			if (ev.button !== 0 || this.readonly || this.saving || m.request) return;
			this.hoverMs = null;
			this.drag = { name: m.name, origMs: m.ms, curMs: m.ms, moved: false };
			this._onMove = (e) => this.dragMove(e);
			this._onUp = (e) => this.dragEnd(e);
			this._onKey = (e) => e.key === "Escape" && this.cancelDrag();
			window.addEventListener("mousemove", this._onMove);
			window.addEventListener("mouseup", this._onUp);
			window.addEventListener("keydown", this._onKey);
			document.body.classList.add("tr-dragging");
		},
		dragMove(ev) {
			if (!this.drag) return;
			const ms = this.eventMs(ev);
			if (ms !== this.drag.curMs) {
				this.drag.curMs = ms;
				if (ms !== this.drag.origMs) this.drag.moved = true;
			}
		},
		dragEnd(ev) {
			if (!this.drag) return;
			const { name, curMs, origMs, moved } = this.drag;
			this.unbindDrag();
			this.drag = null;
			this.suppressPulse();
			if (!moved || curMs === origMs) {
				const checkin = this.row.checkins.find((c) => c.name === name);
				checkin && this.$emit("edit", { checkin, x: ev.clientX, y: ev.clientY });
			} else {
				this.$emit("dragsave", { name, time: this.fmt(curMs) });
			}
		},
		cancelDrag() {
			this.unbindDrag();
			this.drag = null;
			this.suppressPulse();
		},
		suppressPulse() {
			this.suppressClick = true;
			setTimeout(() => (this.suppressClick = false), 0);
		},
		unbindDrag() {
			if (this._onMove) window.removeEventListener("mousemove", this._onMove);
			if (this._onUp) window.removeEventListener("mouseup", this._onUp);
			if (this._onKey) window.removeEventListener("keydown", this._onKey);
			this._onMove = this._onUp = this._onKey = null;
			document.body.classList.remove("tr-dragging");
		},
	},
};
</script>

<style>
body.tr-dragging,
body.tr-dragging * {
	cursor: grabbing !important;
	user-select: none !important;
}
</style>

<style scoped>
.tr-bar {
	position: relative;
	flex: 1;
	height: 32px;
	background: var(--control-bg, #f4f5f6);
	border-radius: 6px;
	cursor: copy;
}
.tr-bar-flagged {
	outline: 1.5px dashed #e8a13c;
	outline-offset: 1px;
}
.tr-bar-empty {
	background: transparent;
	border: 1.5px dashed var(--border-color, #cdd5db);
}
.tr-empty-label {
	position: absolute;
	inset: 0;
	display: flex;
	align-items: center;
	justify-content: center;
	gap: 5px;
	font-size: 11px;
	color: var(--text-muted, #a3adb5);
	pointer-events: none;
}
.tr-seg {
	position: absolute;
	top: 6px;
	bottom: 6px;
	border-radius: 4px;
	pointer-events: none;
	transition: left 0.22s ease, width 0.22s ease;
	display: flex;
	align-items: center;
	justify-content: center;
	overflow: hidden;
}
.tr-seg-dur {
	font-size: 10px;
	font-weight: 600;
	color: #fff;
	white-space: nowrap;
	text-shadow: 0 1px 1px rgba(0, 0, 0, 0.18);
}
.tr-seg-unknown .tr-seg-dur {
	color: #6c7680;
	text-shadow: none;
}
.tr-readonly {
	cursor: default;
}
.tr-readonly .tr-mark {
	cursor: default;
}
/* optimistic save in flight: the punch already sits at its new spot; freeze the
   row (no more edits) and show a quiet progress shimmer until the server row lands */
.tr-saving {
	cursor: progress;
}
.tr-saving .tr-mark {
	pointer-events: none;
	cursor: progress;
}
.tr-saving::after {
	content: "";
	position: absolute;
	inset: 0;
	border-radius: 6px;
	pointer-events: none;
	background: linear-gradient(90deg, transparent 0%, rgba(255, 255, 255, 0.45) 50%, transparent 100%);
	background-size: 200% 100%;
	animation: tr-shimmer 1s linear infinite;
	z-index: 4;
}
@keyframes tr-shimmer {
	0% { background-position: 200% 0; }
	100% { background-position: -200% 0; }
}
.tr-seg-work {
	background: #4caf7d;
}
.tr-seg-break {
	background: #f0b95e;
}
.tr-seg-unknown {
	background: repeating-linear-gradient(45deg, #d7dde2, #d7dde2 5px, #eceff1 5px, #eceff1 10px);
}
/* while dragging, segments + the dragged handle follow the cursor with no ease */
.tr-live .tr-seg {
	transition: none;
}

/* IN/OUT trim handles */
.tr-mark {
	position: absolute;
	top: 3px;
	bottom: 3px;
	width: 8px;
	margin-left: -4px;
	border-radius: 3px;
	z-index: 3;
	cursor: grab;
	box-shadow: 0 1px 3px rgba(15, 23, 42, 0.35);
	transition: left 0.22s ease, transform 0.1s, box-shadow 0.1s;
	/* two faint grip lines */
	background-image: linear-gradient(#ffffffaa, #ffffffaa), linear-gradient(#ffffffaa, #ffffffaa);
	background-size: 1px 10px, 1px 10px;
	background-position: 40% 50%, 60% 50%;
	background-repeat: no-repeat;
}
.tr-mark-in {
	background-color: #1f9d63;
}
.tr-mark-out {
	background-color: #e06a2e;
}
/* span mode: punches between first and last are informational - dim them */
.tr-mark-mid {
	background-color: #94a3b8;
	opacity: 0.5;
	box-shadow: 0 1px 2px rgba(15, 23, 42, 0.2);
}
.tr-mark-mid:hover {
	opacity: 0.85;
}
.tr-mark:hover {
	transform: scaleX(1.35);
}
/* a punch from an Attendance Request: counted, but changed only through the request */
.tr-mark-locked {
	opacity: 0.55;
	cursor: help;
	background-image: none;
}
/* a punch a shift correction replaced: shown where it was, not counted */
.tr-mark-superseded {
	z-index: 2;
	cursor: help;
	background-color: transparent;
	background-image: none;
	border: 1.5px dashed #94a3b8;
	box-shadow: none;
	opacity: 0.8;
}
.tr-mark-drag {
	transition: transform 0.1s;
	transform: scaleX(1.5) scaleY(1.05);
	box-shadow: 0 3px 10px rgba(31, 111, 214, 0.5);
	outline: 2px solid #1f6fd6;
	outline-offset: 1px;
}
.tr-ghost {
	position: absolute;
	top: -3px;
	bottom: -3px;
	width: 1.5px;
	background: #64748b;
	pointer-events: none;
	z-index: 2;
}
.tr-ghost-drag {
	background: #1f6fd6;
	width: 2px;
}
.tr-pill {
	position: absolute;
	bottom: calc(100% + 5px);
	background: #1e293b;
	color: #fff;
	font-size: 11px;
	line-height: 1;
	padding: 5px 8px;
	border-radius: 6px;
	white-space: nowrap;
	pointer-events: none;
	z-index: 10;
}
.tr-pill-drag {
	background: #1f6fd6;
	font-weight: 600;
}
.tr-nowline {
	position: absolute;
	top: -2px;
	bottom: -2px;
	width: 2px;
	background: #e74c3c;
	z-index: 1;
}
@media (max-width: 640px) {
	.tr-bar {
		flex: none;
		height: 40px;
	}
	.tr-mark {
		width: 11px;
		margin-left: -5.5px;
	}
}
</style>
