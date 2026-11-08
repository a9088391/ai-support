<script lang="ts">
export interface KnowledgeSearchResult {
  readonly document_id: string;
  readonly title: string;
  readonly version: string;
  readonly fragment_id: string;
  readonly excerpt: string;
}
</script>

<script setup lang="ts">
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
      <dl class="knowledge-result-details">
        <div>
          <dt>標題</dt>
          <dd>{{ result.title }}</dd>
        </div>
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
    </li>
  </ul>
</template>
