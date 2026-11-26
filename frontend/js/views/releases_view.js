import { api } from "../api.js";
const { ref, computed, onMounted } = Vue;
export const ReleasesView = {
  setup() {
    const official = ref([]); const shadows = ref([]); const envs = ref([]);
    const form = ref({
      flag_id: 3, env_id: 1, kind: "percent", percent: 30, cohort_id: null,
      summary: "开发环境三成灰度试跑", use_shadow: false, approval_no: "",
    });
    const promoteNo = ref({});
    const err = ref(""); const msg = ref("");
    const highRiskEnvs = computed(() => new Set(envs.value.filter(e => e.high_risk || e.approval_required).map(e => e.id)));
    const currentHighRisk = computed(() => highRiskEnvs.value.has(Number(form.value.env_id)));
    async function reload() {
      const [rel, envList] = await Promise.all([api("/api/releases"), api("/api/envs")]);
      official.value = rel.official || []; shadows.value = rel.shadows || [];
      envs.value = envList.items || [];
    }
    async function publish() {
      err.value = ""; msg.value = "";
      try {
        const body = { ...form.value, cohort_id: form.value.cohort_id || null };
        const res = await api("/api/releases/publish", { method: "POST", body: JSON.stringify(body) });
        if (res.mode === "shadow" && res.approval_required) {
          msg.value = "高风险环境已封死乙路径：仅生成影子草案，正式册零增；补审批号后点「升正」才原子生效。";
        } else {
          msg.value = `已提交：${res.mode}`;
        }
        await reload();
      } catch (e) { err.value = e.message || String(e); }
    }
    async function promote(id) {
      err.value = ""; msg.value = "";
      try {
        const res = await api(`/api/releases/shadow/${id}/promote`, {
          method: "POST",
          body: JSON.stringify({ approval_no: (promoteNo.value[id] || "").trim() }),
        });
        msg.value = `升正成功 v${res.version}，审批号 ${res.approval_no || "（低风险免审批）"} 已与正式发布同票落账`;
        promoteNo.value[id] = "";
        await reload();
      } catch (e) { err.value = e.message || String(e); }
    }
    onMounted(reload);
    return { official, shadows, envs, form, promoteNo, err, msg, currentHighRisk, highRiskEnvs, publish, promote, reload };
  },
  template: `
  <div class="grid">
    <div class="panel span-5">
      <h2>发布生效</h2>
      <p class="hint" v-if="currentHighRisk" style="color:#b3541e;font-weight:600">
        高风险环境（prod）：封死乙路径——缺审批号只写影子草案，正式册零增；补号升正后才原子生效。
      </p>
      <p class="hint" v-else>低风险环境：甲路径同事务写正式配置并升版本。勾选影子则走乙路径。</p>
      <div class="field"><label>开关ID</label><input type="number" v-model.number="form.flag_id" /></div>
      <div class="field"><label>环境ID</label><input type="number" v-model.number="form.env_id" /></div>
      <div class="field"><label>类型</label>
        <select v-model="form.kind"><option>percent</option><option>cohort</option><option>percent_and_cohort</option></select>
      </div>
      <div class="field"><label>比例</label><input type="number" v-model.number="form.percent" /></div>
      <div class="field"><label>说明</label><textarea v-model="form.summary" rows="2"></textarea></div>
      <div class="field" v-if="currentHighRisk">
        <label>审批号（发布可空，升正必填）</label>
        <input v-model="form.approval_no" placeholder="如 AP-2026-1126-0001" :disabled="true" />
        <small class="muted">高风险发布先落影子；审批号在右侧「补号升正」处提交。</small>
      </div>
      <div class="field"><label><input type="checkbox" v-model="form.use_shadow" :disabled="currentHighRisk" /> 使用影子草案</label></div>
      <button class="btn" @click="publish">提交发布</button>
      <p class="muted" v-if="msg">{{ msg }}</p>
      <div class="err" v-if="err">{{ err }}</div>
    </div>
    <div class="panel span-7">
      <h2>正式发布（正式册）</h2>
      <table><thead><tr><th>开关</th><th>环境</th><th>版本</th><th>说明</th><th>审批号</th></tr></thead>
        <tbody>
          <tr v-for="r in official" :key="r.id">
            <td>{{ r.flag_key }}</td><td>{{ r.env_code }}</td><td>v{{ r.version }}</td><td>{{ r.summary }}</td>
            <td>
              <span v-if="r.approval_required && !r.approval_no" style="color:#c0392b;font-weight:600">缺审批痕</span>
              <span v-else-if="r.approval_no">{{ r.approval_no }}</span>
              <span v-else class="muted">免审批</span>
            </td>
          </tr>
        </tbody></table>
      <h2 style="margin-top:1rem">影子发布（升正前正式区零改）</h2>
      <table><thead><tr><th>ID</th><th>开关</th><th>环境</th><th>比例</th><th>状态</th><th>审批号/操作</th></tr></thead>
        <tbody>
          <tr v-for="s in shadows" :key="'s'+s.id">
            <td>{{ s.id }}</td><td>{{ s.flag_id }}</td><td>{{ s.env_id }}</td><td>{{ s.percent }}%</td><td>{{ s.status }}</td>
            <td>
              <template v-if="s.status==='open'">
                <input v-model="promoteNo[s.id]" placeholder="补审批号" style="width:11rem"
                       v-if="highRiskEnvs.has(Number(s.env_id))" />
                <button class="btn secondary" @click="promote(s.id)">补号升正</button>
              </template>
            </td>
          </tr>
        </tbody></table>
    </div>
  </div>`
};
