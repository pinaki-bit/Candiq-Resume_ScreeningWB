import { useState, useEffect } from 'react'
import { Search, Filter, Briefcase, MapPin, Award, CheckCircle, Trash2 } from 'lucide-react'
import { getResumes, getResumeDetail } from '../services/screening'
import type { Resume, ResumeDetail } from '../services/screening'
import { PageShell } from '../components/ui/PageShell'
import { LiquidButton } from '../components/ui/liquid-glass-button'
import { getJobs } from '../services/jobs'
import type { Job } from '../services/jobs'
import { api } from '../lib/api'

export function TalentExplorer() {
  const [searchTerm, setSearchTerm] = useState('')
  const [resumes, setResumes] = useState<Resume[]>([])
  const [selectedResume, setSelectedResume] = useState<ResumeDetail | null>(null)
  const [loading, setLoading] = useState(true)
  
  const [jobs, setJobs] = useState<Job[]>([])
  const [assignJobId, setAssignJobId] = useState<number | null>(null)
  const [assigning, setAssigning] = useState(false)

  useEffect(() => {
    fetchResumes()
    getJobs().then(setJobs).catch(console.error)
  }, [])

  const fetchResumes = async () => {
    try {
      const data = await getResumes()
      setResumes(data)
      if (data.length > 0) {
        handleSelectResume(data[0].public_id)
      }
    } catch (error) {
      console.error('Failed to fetch resumes', error)
    } finally {
      setLoading(false)
    }
  }

  const handleSelectResume = async (id: string) => {
    try {
      const detail = await getResumeDetail(id)
      setSelectedResume(detail)
    } catch (error) {
      console.error('Failed to fetch resume detail', error)
    }
  }

  const handleAssignPipeline = async () => {
    if (!selectedResume || !selectedResume.candidate_id) return
    if (!assignJobId) {
      alert('Please select a job from the dropdown first!')
      return
    }
    setAssigning(true)
    try {
      await api.post(`/pipeline/${assignJobId}/add`, {
        candidate_id: selectedResume.candidate_id,
        stage: 'applied'
      })
      alert('Candidate successfully added to pipeline!')
    } catch (error: any) {
      console.error('Failed to assign candidate', error)
      alert(error.response?.data?.detail || 'Failed to assign candidate to pipeline.')
    } finally {
      setAssigning(false)
    }
  }

  const handleDeleteResume = async (e: React.MouseEvent, id: string) => {
    e.stopPropagation() // Prevent selecting the card when clicking delete
    if (!window.confirm('Are you sure you want to remove this resume?')) return
    try {
      await api.delete(`/resumes/${id}`)
      setResumes(prev => prev.filter(r => r.public_id !== id))
      if (selectedResume?.public_id === id) {
        setSelectedResume(null)
      }
    } catch (error: any) {
      console.error('Failed to delete resume', error)
      alert(error.response?.data?.detail || 'Failed to remove resume.')
    }
  }

  const handleDeleteAll = async () => {
    if (!window.confirm('Are you sure you want to delete ALL resumes? This action cannot be undone.')) return
    const ids = resumes.map(r => r.public_id)
    try {
      for (const id of ids) {
        await api.delete(`/resumes/${id}`)
      }
      setResumes([])
      setSelectedResume(null)
      alert('All resumes have been successfully deleted.')
    } catch (error: any) {
      console.error('Failed to delete all resumes', error)
      alert('Some resumes failed to delete. Please try again.')
      fetchResumes()
    }
  }

  // Filter based on candidate name or skills (if available in summary list)
  const filteredResumes = resumes.filter(r => 
    r.original_filename.toLowerCase().includes(searchTerm.toLowerCase())
  )

  const actionButton = (
    <LiquidButton className="px-4 py-2.5 bg-[#3A2C6E]/60 border border-[#F6B98A]/25 rounded-full text-sm font-semibold text-[#FBE6B8] flex items-center gap-2 shadow-md">
      <Filter className="w-4 h-4 text-[#F6B98A]" />
      Advanced Filters
    </LiquidButton>
  )

  return (
    <PageShell
      title="Talent Intelligence Search"
      subtitle="Discover and analyze candidate profiles with AI matching."
      action={actionButton}
    >
      <div className="w-full flex flex-col gap-6">
        {/* Search Bar & Actions */}
        <div className="relative w-full flex items-center gap-3">
          <div className="relative flex-1">
            <div className="absolute inset-y-0 left-0 pl-4 flex items-center pointer-events-none">
              <Search className="h-5 w-5 text-[#F6B98A]/70" />
            </div>
            <input
              type="text"
              className="w-full pl-11 pr-4 py-3.5 bg-[#3A2C6E]/40 border border-[#F6B98A]/20 rounded-xl text-[#FBE6B8] placeholder-[#FBE6B8]/45 focus:outline-none focus:ring-2 focus:ring-[#F6B98A]/40 focus:border-transparent transition-all shadow-lg shadow-black/20 text-sm sm:text-base"
              placeholder="Search by filename or keywords..."
              value={searchTerm}
              onChange={(e) => setSearchTerm(e.target.value)}
            />
          </div>
          {resumes.length > 0 && (
             <button
                onClick={handleDeleteAll}
                className="shrink-0 flex items-center gap-2 px-4 py-3.5 bg-red-500/10 hover:bg-red-500/20 text-red-400 border border-red-500/20 rounded-xl transition-colors font-semibold text-sm cursor-pointer"
             >
                <Trash2 className="w-4 h-4" />
                <span className="hidden sm:inline">Delete All</span>
             </button>
          )}
        </div>

        {/* Responsive Content Grid */}
        <div className="w-full grid grid-cols-1 lg:grid-cols-12 gap-6 items-start">
          {/* Candidate List (4/12 Cols) */}
          <div className="lg:col-span-4 w-full flex flex-col gap-3 h-[calc(100vh-260px)] overflow-y-auto pr-2 custom-scrollbar">
            {loading ? (
               <div className="text-[#FBE6B8]/60 p-4">Loading candidates...</div>
            ) : filteredResumes.length === 0 ? (
               <div className="text-[#FBE6B8]/60 p-4">No candidates found. Upload a resume first.</div>
            ) : (
              filteredResumes.map((resume) => (
                <div 
                  key={resume.public_id} 
                  onClick={() => handleSelectResume(resume.public_id)}
                  className={`shrink-0 glass-card rounded-xl p-4 cursor-pointer border relative overflow-hidden group transition-all ${selectedResume?.public_id === resume.public_id ? 'border-[#F6B98A] bg-[#C4749B]/25 shadow-[0_0_20px_rgba(246,185,138,0.2)]' : 'border-[#F6B98A]/15 bg-[#3A2C6E]/40 hover:bg-[#C4749B]/15'}`}
                >
                  <div className="absolute top-2 right-2 flex flex-col gap-2">
                     <div className="flex items-center justify-center w-7 h-7 rounded-full bg-[#181130]/80 border border-[#F6B98A]/30 shadow-[0_0_10px_rgba(246,185,138,0.25)]">
                        <CheckCircle className="w-3.5 h-3.5 text-[#F6B98A]" />
                     </div>
                     <button
                        onClick={(e) => handleDeleteResume(e, resume.public_id)}
                        className="flex items-center justify-center w-7 h-7 rounded-full bg-[#181130]/80 border border-red-500/30 text-red-400 hover:bg-red-500/20 hover:text-red-300 transition-colors z-10"
                        title="Remove Candidate"
                     >
                        <Trash2 className="w-3.5 h-3.5" />
                     </button>
                  </div>
                  <div className="pr-10">
                    <h3 className="text-sm font-semibold text-[#FBE6B8] mb-1.5 truncate leading-tight">{resume.original_filename}</h3>
                    <p className="text-xs font-semibold text-[#F6B98A] mb-1">Status: {resume.status}</p>
                    <p className="text-[10px] text-[#FBE6B8]/60">Uploaded: {new Date(resume.uploaded_at).toLocaleDateString()}</p>
                  </div>
                </div>
              ))
            )}
          </div>

          {/* Candidate Intelligence Detail (8/12 Cols) */}
          <div className="lg:col-span-8 w-full glass-card rounded-2xl flex flex-col border border-[#F6B98A]/15 shadow-2xl overflow-hidden h-[calc(100vh-260px)]">
            {selectedResume ? (
              <>
                {/* Header */}
                <div className="p-3 sm:p-4 border-b border-[#F6B98A]/15 bg-gradient-to-b from-[#3A2C6E]/60 to-[#281B4B]/40">
                  <div className="flex flex-col sm:flex-row justify-between items-start gap-3">
                    <div className="flex-1">
                      <h2 className="text-base sm:text-lg font-bold text-[#FBE6B8] mb-1.5 truncate">{selectedResume.original_filename}</h2>
                      <div className="flex flex-wrap items-center gap-3 sm:gap-4 text-xs text-[#FBE6B8]/80">
                        <span className="text-[#F6B98A] font-medium">{selectedResume.predicted_domain || 'Domain Pending'}</span>
                        <div className="flex items-center gap-1.5">
                            <Briefcase className="w-3.5 h-3.5 text-[#F6B98A]" />
                            {selectedResume.prediction_confidence || 'N/A'}
                        </div>
                        <div className="flex items-center gap-1.5">
                            <MapPin className="w-3.5 h-3.5 text-[#F6B98A]" />
                            {(selectedResume.file_size_bytes / 1024).toFixed(1)} KB
                        </div>
                      </div>
                    </div>
                    <div className="sm:text-right flex flex-col items-end shrink-0">
                      <div className="flex items-center gap-3 mb-1.5">
                        <div className="text-right">
                          <div className="text-sm font-bold text-transparent bg-clip-text bg-gradient-to-r from-[#C4749B] to-[#F6B98A] leading-none mb-0.5">
                            {selectedResume.status}
                          </div>
                          <p className="text-[9px] text-[#FBE6B8]/60 uppercase tracking-wider leading-none">Status</p>
                        </div>
                      </div>
                      
                      {/* Pipeline Assignment */}
                      {selectedResume.candidate_id && (
                        <div className="flex items-center gap-2">
                          <select
                            value={assignJobId || ''}
                            onChange={(e) => setAssignJobId(Number(e.target.value))}
                            className="appearance-none pl-2 pr-7 py-1 h-[26px] rounded-md text-[11px] font-medium text-[#FBE6B8] outline-none cursor-pointer border border-[rgba(251,230,184,0.18)] bg-[rgba(58,44,110,0.45)]"
                          >
                            <option value="">Select Job...</option>
                            {jobs.map(j => <option key={j.id} value={j.id}>{j.title}</option>)}
                          </select>
                          <LiquidButton
                            size="sm"
                            onClick={handleAssignPipeline}
                            disabled={assigning}
                            className="px-3 py-1 h-[26px] text-[11px] rounded-md bg-gradient-to-r from-[#F6B98A] to-[#C4749B] text-[#140F25] font-semibold disabled:opacity-50 border-0"
                          >
                            {assigning ? 'Adding...' : 'Add to Pipeline'}
                          </LiquidButton>
                        </div>
                      )}
                    </div>
                  </div>
                </div>

                <div className="p-4 sm:p-5 flex-1 overflow-y-auto custom-scrollbar space-y-5">
                  {/* Skill Heatmap */}
                  <div>
                    <h3 className="text-lg font-semibold text-[#FBE6B8] mb-4 flex items-center gap-2">
                      <Award className="w-5 h-5 text-[#F6B98A]" />
                      Extracted Skills Intelligence
                    </h3>
                    {selectedResume.extracted_skills && selectedResume.extracted_skills.length > 0 ? (
                      <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
                        {selectedResume.extracted_skills.map(skill => (
                          <div key={skill.id} className="bg-[#3A2C6E]/40 p-4 rounded-xl border border-[#F6B98A]/15">
                            <div className="flex justify-between mb-2">
                              <span className="text-sm font-medium text-[#FBE6B8]">{skill.canonical_name}</span>
                              <span className="text-sm font-bold text-[#F6B98A]">{skill.frequency} mentions</span>
                            </div>
                            <div className="text-xs text-[#FBE6B8]/60 mb-2 truncate">
                              {skill.category || 'Uncategorized'} - {skill.domain || 'Generic'}
                            </div>
                            <div className="w-full bg-[#181130]/80 rounded-full h-1.5 overflow-hidden border border-[#F6B98A]/10">
                              <div className="h-1.5 rounded-full bg-gradient-to-r from-[#C4749B] to-[#F6B98A]" style={{ width: `${Math.min(100, skill.frequency * 20)}%` }} />
                            </div>
                          </div>
                        ))}
                      </div>
                    ) : (
                      <p className="text-[#FBE6B8]/60 text-sm">No skills were extracted or extraction is still pending.</p>
                    )}
                  </div>
                </div>
              </>
            ) : (
              <div className="p-8 flex items-center justify-center h-full text-[#FBE6B8]/50 min-h-[300px]">
                Select a candidate from the list to view intelligence details.
              </div>
            )}
          </div>
        </div>
      </div>
    </PageShell>
  )
}

