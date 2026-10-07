{{- define "dsp.name" -}}{{ .Chart.Name }}{{- end -}}

{{- define "dsp.fullname" -}}
{{- if contains .Chart.Name .Release.Name -}}{{ .Release.Name | trunc 40 | trimSuffix "-" }}
{{- else -}}{{ printf "%s-%s" .Release.Name .Chart.Name | trunc 40 | trimSuffix "-" }}{{- end -}}
{{- end -}}

{{- define "dsp.labels" -}}
app.kubernetes.io/name: {{ include "dsp.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version }}
{{- end -}}

{{- define "dsp.selector" -}}
app.kubernetes.io/name: {{ include "dsp.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/component: api
{{- end -}}

{{- define "dsp.pgSelector" -}}
app.kubernetes.io/name: {{ include "dsp.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/component: postgres
{{- end -}}

{{- define "dsp.backupSelector" -}}
app.kubernetes.io/name: {{ include "dsp.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/component: backup
{{- end -}}

{{- define "dsp.secretName" -}}{{ default (printf "%s-config" (include "dsp.fullname" .)) .Values.secrets.existingSecret }}{{- end -}}
{{- define "dsp.pgSecretName" -}}{{ printf "%s-postgres" (include "dsp.fullname" .) }}{{- end -}}
{{- define "dsp.pgHost" -}}{{ printf "%s-postgres" (include "dsp.fullname" .) }}{{- end -}}

{{- define "dsp.image" -}}
{{- if .Values.image.digest -}}{{ .Values.image.repository }}@{{ .Values.image.digest }}
{{- else -}}{{ .Values.image.repository }}:{{ .Values.image.tag | default .Chart.AppVersion }}{{- end -}}
{{- end -}}

{{/* Значение секрета: заданное вручную -> уже существующее в кластере (не меняется при upgrade) -> сгенерированное. */}}
{{- define "dsp.stable" -}}
{{- $root := index . 0 -}}{{- $secretName := index . 1 -}}{{- $key := index . 2 -}}{{- $provided := index . 3 -}}{{- $gen := index . 4 -}}
{{- if $provided -}}{{ $provided }}
{{- else -}}
{{- $existing := lookup "v1" "Secret" $root.Release.Namespace $secretName -}}
{{- if and $existing $existing.data (hasKey $existing.data $key) -}}{{ index $existing.data $key | b64dec }}{{- else -}}{{ $gen }}{{- end -}}
{{- end -}}
{{- end -}}
