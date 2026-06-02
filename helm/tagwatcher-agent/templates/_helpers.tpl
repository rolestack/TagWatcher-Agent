{{- define "tagwatcher-agent.name" -}}
{{- .Release.Name | trunc 63 | trimSuffix "-" }}
{{- end }}

{{- define "tagwatcher-agent.serviceAccountName" -}}
{{- if .Values.serviceAccount.name }}
{{- .Values.serviceAccount.name }}
{{- else }}
{{- include "tagwatcher-agent.name" . }}
{{- end }}
{{- end }}

{{- define "tagwatcher-agent.clusterRoleName" -}}
{{- if .Values.clusterRole.name }}
{{- .Values.clusterRole.name }}
{{- else }}
{{- include "tagwatcher-agent.name" . }}
{{- end }}
{{- end }}

{{- define "tagwatcher-agent.secretName" -}}
{{- if .Values.secret.create }}
{{- required "secret.name is required when secret.create=true" .Values.secret.name }}
{{- else }}
{{- include "tagwatcher-agent.name" . }}
{{- end }}
{{- end }}
