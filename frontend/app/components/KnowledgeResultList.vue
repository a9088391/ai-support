<script setup lang="ts">
import type { KnowledgeSearchResult } from "~/workbench/contracts";
defineProps<{ results: readonly KnowledgeSearchResult[] }>();
</script>

<template>
  <ul class="knowledge-result-list">
    <!-- 清單依本次結果順序呈現，同一片段的不同版本可以同時出現。 -->
    <li
      v-for="(result, position) in results"
      :key="position"
      class="knowledge-result"
    >
      <p class="knowledge-result-title">{{ result.title }}</p>
      <!-- 預覽僅視覺縮行且不重複朗讀；完整證據保留在原生展開區。 -->
      <p class="knowledge-result-preview" aria-hidden="true">
        {{ result.excerpt }}
      </p>
      <details class="knowledge-result-source">
        <summary
          :aria-label="`查看完整來源與授權：${result.title}（${result.document_id} / ${result.version} / ${result.fragment_id}）`"
        >
          查看完整來源與授權
        </summary>
        <dl class="knowledge-result-details">
          <div>
            <dt>版本</dt>
            <dd>
              <code>{{ result.version }}</code>
            </dd>
          </div>
          <div>
            <dt>來源</dt>
            <dd>
              文件 <code>{{ result.document_id }}</code
              >，片段 <code>{{ result.fragment_id }}</code>
            </dd>
          </div>
          <div class="knowledge-result-excerpt">
            <dt>完整摘錄</dt>
            <dd>{{ result.excerpt }}</dd>
          </div>
        </dl>
      </details>
    </li>
  </ul>
</template>
