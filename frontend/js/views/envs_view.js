import { api } from "../api.js";
const { ref, onMounted } = Vue;
export const EnvsView = {
  setup() {
    const items = ref([]); const err = ref("");
    async function reload() {
      try { items.value = (await api("/api/envs")).items || []; }
      catch (e) { err.value = e.message || String(e); }
    }
    onMounted(reload);
    return { items, err, reload };
  },
  template: `
  <div class="panel">
    <h2>环境</h2>
    <p class="hint">高风险环境（风险等级 ≥ 3，即 prod）封死乙路径：发布缺审批号只落影子草案、正式册零增，补号后原子升正。</p>
    <div class="err" v-if="err">{{ err }}</div>
    <table>
      <thead><tr><th>代码</th><th>名称</th><th>风险等级</th><th>高风险</th><th>发布审批号</th></tr></thead>
      <tbody>
        <tr v-for="it in items" :key="it.id">
          <td>{{ it.code }}</td><td>{{ it.title }}</td><td>{{ it.risk_level }}</td>
          <td>
            <span v-if="it.high_risk" style="color:#c0392b;font-weight:700">是</span>
            <span v-else class="muted">否</span>
          </td>
          <td>
            <span v-if="it.approval_required" style="color:#b3541e;font-weight:600">必需（缺号正式零增）</span>
            <span v-else class="muted">免审批</span>
          </td>
        </tr>
      </tbody>
    </table>
  </div>`
};
